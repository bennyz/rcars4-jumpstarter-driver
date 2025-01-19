import sys
import time

from jumpstarter.client.adapters import PexpectAdapter

from uboot import DhcpInfo


class ConsoleHelper:
    def __init__(self, console, expected_str=None, delay=0):
        self.console = console
        self.expected_str: str | None = expected_str
        self.delay = delay

    def send_command(self, cmd: str, expect_str=None, timeout: int = 30, delay: int = 0):
        self.console.sendline(cmd)
        time.sleep(delay or self.delay)

        if expect_str is None and self.expected_str:
            expect_str = self.expected_str

        if expect_str:
            self.console.expect(expect_str, timeout=timeout)
            if self.console.before is not None:
                return self.console.before.decode('utf-8')
        return ""

    def wait_for_pattern(self, pattern: str, timeout: int = 60):
        self.console.expect(pattern, timeout=timeout)
        return (self.console.before + self.console.after).decode('utf-8')

    def get_dhcp_info(self, timeout: int = 60) -> DhcpInfo:
        print("\nRunning DHCP to obtain network configuration...")
        self.send_command("dhcp")

        self.console.expect("DHCP client bound to address ([0-9.]+)", timeout=timeout)
        ip_address = self.console.match.group(1).decode('utf-8')

        self.console.expect("sending through gateway ([0-9.]+)", timeout=timeout)
        gateway = self.console.match.group(1).decode('utf-8')
        self.wait_for_pattern("=>")
        self.send_command("printenv netmask")
        try:
            self.console.expect("netmask=([0-9.]+)", timeout=timeout)
            netmask = self.console.match.group(1).decode('utf-8')
        except Exception:
            print("Could not get netmask, using default 255.255.255.0")
            netmask = "255.255.255.0"

        self.wait_for_pattern("=>")

        return DhcpInfo(ip_address=ip_address, gateway=gateway, netmask=netmask)

    def set_env(self, key: str, value: str):
        self.send_command(f"setenv {key} '{value}'", "=>", timeout=10)

def restart_from_initramfs(console):
    helper = ConsoleHelper(console)
    commands = [
        "modprobe renesas_wdt",
        "watchdog -T 2 -t 120 /dev/watchdog",
    ]

    for cmd in commands:
        helper.send_command(cmd, "/ #")

def initramfs_shell_flash(console, http_url, dhcp_info: DhcpInfo):
    helper = ConsoleHelper(console)

    commands = [
        "ip link set dev tsn0 up",
        f"ip addr add {dhcp_info.ip_address}/{dhcp_info.cidr} dev tsn0",
        f"ip route add default via {dhcp_info.gateway}",
    ]

    for cmd in commands:
        helper.send_command(cmd, "/ #")

    flash_cmd = f'wget -O - "{http_url}/target.gz" | zcat | dd of=/dev/mmcblk0 bs=64K iflag=fullblock'
    helper.send_command(flash_cmd)
    console.expect(r"\d+ bytes \(.+?\) copied, [0-9.]+ seconds, .+?", timeout=600)
    console.expect("/ #", timeout=30)

def upload_files_to_tftp(tftp, test_files):
    for filename in test_files:
        print(f"Uploading {filename} to TFTP server...")
        tftp.put_local_file(filename)

def upload_target_to_http(http_client, filename="target.gz", local_filepath="target.gz"):
    try:
        existing_files = http_client.list_files()
        if filename in existing_files:
            print(f"File '{filename}' already exists on the server.")
            return True

        print(f"Uploading '{filename}' to the HTTP server...")
        uploaded_filename = http_client.put_local_file(local_filepath)
        print(f"File '{uploaded_filename}' uploaded successfully.")
        return True

    except Exception as e:
        print(f"Error uploading file to HTTP server: {e}")
        return False

def setup_environment(client, skip_flash=False):
    gpio = client.gpio
    serial = client.serial
    tftp = client.tftp
    http = client.http
    if not skip_flash:
      tftp_host = tftp.get_host()

      http.start()
      upload_target_to_http(http, filename="target.gz")

      test_files = ["Image", "r8a779f0-spider.dtb", "initramfs-debug.img"]
      tftp.start()
      upload_files_to_tftp(tftp, test_files)

      print("Turning power off")
      gpio.off()
      time.sleep(3)
      print("Turning power on")
      gpio.on()

      with PexpectAdapter(client=serial) as console:
          console.logfile = sys.stdout.buffer
          print("Attempting to interrupt boot sequence...")
          for _ in range(10):
              console.send(b'\r')
              time.sleep(0.1)

          console.expect("=>", timeout=60)

          helper = ConsoleHelper(console)
          dhcp_info = helper.get_dhcp_info()

          env_vars = {
              "ipaddr": dhcp_info.ip_address,
              "serverip": tftp_host,
              "fdtaddr": "0x48000000",
              "ramdiskaddr": "0x48080000",
              "boot_tftp": "tftp ${fdtaddr} ${fdtfile}; tftp ${loadaddr} ${bootfile}; tftp ${ramdiskaddr} ${ramdiskfile}; booti ${loadaddr} ${ramdiskaddr} ${fdtaddr}",
              "bootfile": "Image",
              "fdtfile": "r8a779f0-spider.dtb",
              "ramdiskfile": "initramfs-debug.img"
          }

          for key, value in env_vars.items():
              helper.set_env(key, value)

          helper.send_command("run boot_tftp", '/ #', timeout=3000)

          initramfs_shell_flash(console, http.get_url(), dhcp_info)
          restart_from_initramfs(console)

          console.expect("=>", timeout=60)

          boot_env = {
              "bootcmd": "if part number mmc 0 boot boot_part; then run boot_grub; else run boot_aboot; fi",
              "boot_aboot": "mmc dev 0; part start mmc 0 boot_a boot_start; part size mmc 0 boot_a boot_size; mmc read $loadaddr $boot_start $boot_size; abootimg get dtb --index=0 dtb0_start dtb0_size; setenv bootargs androidboot.slot_suffix=_a; bootm $loadaddr $loadaddr $dtb0_start",
              "boot_grub": "ext4load mmc 0:${boot_part} 0x48000000 dtb/renesas/r8a779f0-spider.dtb; fatload mmc 0:1 0x70000000 /EFI/BOOT/BOOTAA64.EFI && bootefi 0x70000000 0x48000000"
          }

          for key, value in boot_env.items():
              helper.set_env(key, value)

          helper.send_command("boot")
          console.expect("login:", timeout=300)
          helper.send_command("root", expect_str="Password:", timeout=10)
          helper.send_command("password", expect_str="#", timeout=10)

    return client

def teardown_environment(client):
    http = client.http
    tftp = client.tftp

    http.stop()
    tftp.stop()
