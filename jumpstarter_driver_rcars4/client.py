import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import asyncclick as click
from jumpstarter_driver_composite.client import CompositeClient
from jumpstarter_driver_network.adapters import PexpectAdapter
from opendal import Operator

from jumpstarter.client import DriverClient


@dataclass(kw_only=True)
class RCarSetupClient(CompositeClient, DriverClient):
    def flash(self, initramfs: str, kernel: str, dtb: str, os_image: str):
        self.logger.setLevel(logging.DEBUG)
        self.tftp.start()
        self.http.start()

        self.logger.info("Starting TFTP uploads...")

        # Upload initramfs, kernel, and dtb to TFTP
        for path_info in [("initramfs", initramfs), ("kernel", kernel), ("dtb", dtb)]:
            file_type, path = path_info
            filename = Path(path).name if not path.startswith(('http://', 'https://')) else path.split('/')[-1]
            self.logger.info(f"Uploading {filename} to TFTP server")

            # check if file is already uploaded
            if self.tftp.storage.exists(filename):
                self.logger.info(f"{filename} already uploaded")
                continue

            # Handle HTTP URLs with OpenDAL
            if path.startswith(('http://', 'https://')):
                self.logger.info(f"Downloading {file_type} from URL: {path}")
                parsed_url = urlparse(path)
                operator = Operator(
                    'http',
                    root='/',
                    endpoint=f"{parsed_url.scheme}://{parsed_url.netloc}"
                )
                remote_path = parsed_url.path
                if remote_path.startswith('/'):
                    remote_path = remote_path[1:]

                # Use OpenDAL to transfer from remote HTTP to local TFTP
                self.tftp.storage.write_from_path(filename, remote_path, operator)
            else:
                self.tftp.storage.write_from_path(filename, path)

            self.logger.info(f"Completed TFTP upload of {filename}")

        # Upload OS image to HTTP server
        self.logger.info("Starting HTTP upload...")
        os_image_name = Path(os_image).name if not os_image.startswith(('http://', 'https://')) else os_image.split('/')[-1]
        self.logger.info(f"Uploading {os_image_name} to HTTP server")

        # Check if file already exists on HTTP server
        if self.http.storage.exists(os_image_name):
            self.logger.info(f"OS image {os_image_name} already exists on HTTP server")
        else:
            if os_image.startswith(('http://', 'https://')):
                from urllib.parse import urlparse

                from opendal import Operator

                parsed_url = urlparse(os_image)
                operator = Operator(
                    'http',
                    root='/',
                    endpoint=f"{parsed_url.scheme}://{parsed_url.netloc}"
                )
                remote_path = parsed_url.path
                if remote_path.startswith('/'):
                    remote_path = remote_path[1:]

                self.http.storage.write_from_path(os_image_name, remote_path, operator)
            else:
                self.http.storage.write_from_path(os_image_name, os_image)

        # Get the full URL to the uploaded image
        http_url = self.http.get_url()
        image_url = f"{http_url}/{os_image_name}"
        self.logger.info(f"OS image available at: {image_url}")

        try:
            # Call the power_cycle method through the driver API
            self.logger.info("Power cycling RCar device...")
            self.power.off()
            time.sleep(3)
            self.power.on()

            with PexpectAdapter(client=self.children["serial"]) as console:
                console.logfile = sys.stdout.buffer

                self.logger.info("Waiting for U-Boot prompt...")
                for _ in range(20):
                    console.sendline("\r\n")
                    time.sleep(0.1)

                console.expect("=>", timeout=60)

                self.logger.info("Configuring network...")
                console.sendline("dhcp")
                console.expect("DHCP client bound to address ([0-9.]+)")
                ip_address = console.match.group(1).decode('utf-8')

                console.expect("sending through gateway ([0-9.]+)")
                gateway = console.match.group(1).decode('utf-8')
                console.expect("=>")

                tftp_host = self.tftp.get_host()
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
                    "bootfile": Path(kernel).name if not kernel.startswith(('http://', 'https://')) else kernel.split('/')[-1],
                    "fdtfile": Path(dtb).name if not dtb.startswith(('http://', 'https://')) else dtb.split('/')[-1],
                    "ramdiskfile": Path(initramfs).name if not initramfs.startswith(('http://', 'https://')) else initramfs.split('/')[-1]
                }

                for key, value in env_vars.items():
                    self.logger.info(f"Setting env {key}={value}")
                    console.sendline(f"setenv {key} '{value}'")
                    console.expect("=>")

                self.logger.info("Booting into initramfs...")
                console.sendline("run boot_tftp")
                console.expect("/ #", timeout=1000)

                self.logger.info("Configuring initramfs network...")
                for cmd in [
                    "ip link set dev tsn0 up",
                    f"ip addr add {ip_address}/24 dev tsn0",
                    f"ip route add default via {gateway}"
                ]:
                    console.sendline(cmd)
                    console.expect("/ #")

                self.logger.info("Flashing OS image...")
                # Determine if the image needs decompression
                decompress_cmd = _get_decompression_command(os_image_name)

                flash_cmd = (
                    f'wget -O - "{image_url}" | '
                    f'{decompress_cmd} | dd of=/dev/mmcblk0 bs=64K iflag=fullblock'
                )
                console.sendline(flash_cmd)
                console.expect(r"\d+ bytes \(.+?\) copied, [0-9.]+ seconds, .+?", timeout=600)
                console.expect("/ #")

                # Use the power_cycle method from the driver
                self.logger.info("Power cycling RCar device again...")
                self.power.off()
                time.sleep(3)
                self.power.on()

                self.logger.info("Waiting for reboot...")
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
                    self.logger.info(f"Setting boot env {key}")
                    console.sendline(f"setenv {key} '{value}'")
                    console.expect("=>")

                console.sendline("saveenv")
                console.expect("=>", timeout=5)

                self.logger.info("Performing final boot...")
                console.sendline("boot")
                console.expect("login:", timeout=300)
                console.sendline("root")
                console.expect("Password:")
                console.sendline("password")
                console.expect("#")

                return "Flash and boot completed successfully"

        except Exception as e:
            self.logger.error(f"Flash failed: {str(e)}")
            raise

    def power_cycle(self):
        """Implementation of power cycling for testing/debugging"""
        self.logger.info("Power cycling RCar device using GPIO")
        self.power.off()
        time.sleep(3)
        self.power.on()
        return {
            "tftp_host": self.tftp.get_host(),
            "http_url": self.http.get_url()
        }

    def cli(self):
        @click.group()
        def base():
            """RCar Driver"""
            pass

        @base.command()
        @click.option('--kernel', required=True,
                      help='Linux kernel ARM64 boot executable (uncompressed Image) - local path or URL')
        @click.option('--initramfs', required=True,
                      help='Initial RAM filesystem (uImage format) - local path or URL')
        @click.option('--dtb', required=True,
                      help='Device Tree Binary file - local path or URL')
        @click.option('--os-image', required=True,
                      help='Operating system image to flash - local path or URL')
        def flash(kernel, initramfs, dtb, os_image):
            try:
                result = self.flash(initramfs, kernel, dtb, os_image)
                click.echo(result)
                sys.exit(0)
            except Exception as e:
                self.logger.error(f"Flash failed: {str(e)}")
                sys.exit(1)

        for name, child in self.children.items():
            if hasattr(child, "cli"):
                base.add_command(child.cli(), name)

        return base


def _get_decompression_command(filename: str) -> str:
    """
    Determine the appropriate decompression command based on file extension

    Args:
        filename (str): Name of the file to check

    Returns:
        str: Decompression command ('zcat', 'xzcat', or 'cat' for uncompressed)
    """
    filename = filename.lower()
    if filename.endswith(('.gz', '.gzip')):
        return 'zcat'
    elif filename.endswith('.xz'):
        return 'xzcat'
    return 'cat'
