from dataclasses import dataclass
from pathlib import Path
import logging
import asyncclick as click
import sys
import time

from jumpstarter.client import DriverClient
from jumpstarter.drivers.composite.client import CompositeClient
from jumpstarter.client.adapters import PexpectAdapter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass(kw_only=True)
class RCarSetupClient(CompositeClient, DriverClient):
    def flash(self, initramfs: str, kernel: str, dtb: str, os_image: str):
        self.children["tftp"].start()
        self.children["http"].start()

        existing_tftp_files = self.children["tftp"].list_files()
        for path in [initramfs, kernel, dtb]:
            filename = Path(path).name
            if filename not in existing_tftp_files:
                logger.info(f"Putting {path} in TFTP server")
                self.children["tftp"].put_local_file(path)
            else:
                logger.info(f"File {filename} already exists on TFTP server")

        existing_http_files = self.children["http"].list_files()
        os_image_name = Path(os_image).name
        if os_image_name not in existing_http_files:
            logger.info(f"Putting {os_image} in HTTP server")
            self.children["http"].put_local_file(os_image)
        else:
            logger.info(f"File {os_image_name} already exists on HTTP server")

        try:
            self.call("power_cycle")
            with PexpectAdapter(client=self.children["serial"]) as console:
                console.logfile = sys.stdout.buffer

                logger.info("Waiting for U-Boot prompt...")

                for _ in range(20):
                    console.sendline("\r\n")
                    time.sleep(0.1)

                console.expect("=>", timeout=60)

                logger.info("Configuring network...")
                console.sendline("dhcp")
                console.expect("DHCP client bound to address ([0-9.]+)")
                ip_address = console.match.group(1).decode('utf-8')

                console.expect("sending through gateway ([0-9.]+)")
                gateway = console.match.group(1).decode('utf-8')
                console.expect("=>")

                tftp_host = self.children["tftp"].get_host()
                env_vars = {
                    "ipaddr": ip_address,
                    "serverip": tftp_host,
                    "fdtaddr": "0x48000000",
                    "ramdiskaddr": "0x48080000",
                    "boot_tftp": (
                        "tftp ${fdtaddr} ${fdtfile}; "
                        "tftp ${loadaddr} ${bootfile}; "
                        "tftp ${ramdiskaddr} ${ramdiskfile}; "
                        "booti ${loadaddr} ${ramdiskaddr} ${fdtaddr}"
                    ),
                    "bootfile": Path(kernel).name,
                    "fdtfile": Path(dtb).name,
                    "ramdiskfile": Path(initramfs).name
                }

                for key, value in env_vars.items():
                    logger.info(f"Setting env {key}={value}")
                    console.sendline(f"setenv {key} '{value}'")
                    console.expect("=>")

                logger.info("Booting into initramfs...")
                console.sendline("run boot_tftp")
                console.expect("/ #", timeout=1000)

                logger.info("Configuring initramfs network...")
                for cmd in [
                    "ip link set dev tsn0 up",
                    f"ip addr add {ip_address}/24 dev tsn0",
                    f"ip route add default via {gateway}"
                ]:
                    console.sendline(cmd)
                    console.expect("/ #")

                logger.info("Flashing OS image...")
                http_url = self.children["http"].get_url()
                flash_cmd = (
                    f'wget -O - "{http_url}/{Path(os_image).name}" | '
                    f'zcat | dd of=/dev/mmcblk0 bs=64K iflag=fullblock'
                )
                console.sendline(flash_cmd)
                console.expect(r"\d+ bytes \(.+?\) copied, [0-9.]+ seconds, .+?", timeout=600)
                console.expect("/ #")

                self.call("power_cycle")

                logger.info("Waiting for reboot...")
                for _ in range(20):
                    console.sendline("")
                    time.sleep(0.1)
                console.expect("=>", timeout=60)

                boot_env = {
                    "bootcmd": (
                        "if part number mmc 0 boot boot_part; then "
                        "run boot_grub; else run boot_aboot; fi"
                    ),
                    "boot_aboot": (
                        "mmc dev 0; "
                        "part start mmc 0 boot_a boot_start; "
                        "part size mmc 0 boot_a boot_size; "
                        "mmc read $loadaddr $boot_start $boot_size; "
                        "abootimg get dtb --index=0 dtb0_start dtb0_size; "
                        "setenv bootargs androidboot.slot_suffix=_a; "
                        "bootm $loadaddr $loadaddr $dtb0_start"
                    ),
                    "boot_grub": (
                        "ext4load mmc 0:${boot_part} 0x48000000 "
                        "dtb/renesas/r8a779f0-spider.dtb; "
                        "fatload mmc 0:1 0x70000000 /EFI/BOOT/BOOTAA64.EFI && "
                        "bootefi 0x70000000 0x48000000"
                    )
                }

                for key, value in boot_env.items():
                    logger.info(f"Setting boot env {key}")
                    console.sendline(f"setenv {key} '{value}'")
                    console.expect("=>")

                console.sendline("saveenv")
                console.expect("=>", timeout=5)

                logger.info("Performing final boot...")
                console.sendline("boot")
                console.expect("login:", timeout=300)
                console.sendline("root")
                console.expect("Password:")
                console.sendline("password")
                console.expect("#")

                return "Flash and boot completed successfully"

        except Exception as e:
            logger.error(f"Flash failed: {str(e)}")
            raise

    def cli(self):
        @click.group()
        def rcar():
            pass

        @rcar.command()
        @click.option('--kernel', required=True, type=click.Path(exists=True),
                     help='Linux kernel ARM64 boot executable (uncompressed Image)')
        @click.option('--initramfs', required=True, type=click.Path(exists=True),
                     help='Initial RAM filesystem (uImage format)')
        @click.option('--dtb', required=True, type=click.Path(exists=True),
                     help='Device Tree Binary file')
        @click.option('--os-image', required=True, type=click.Path(exists=True),
                     help='Operating system image to flash')
        def flash(kernel, initramfs, dtb, os_image):
            result = self.flash(
                str(Path(initramfs).resolve()),
                str(Path(kernel).resolve()),
                str(Path(dtb).resolve()),
                str(Path(os_image).resolve())
            )
            click.echo(result)

        for name, child in self.children.items():
            if hasattr(child, "cli"):
                rcar.add_command(child.cli(), name)

        return rcar
