import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class DhcpInfo:
    ip_address: str
    gateway: str
    netmask: str

    @property
    def cidr(self) -> str:
        try:
            octets = [int(x) for x in self.netmask.split('.')]
            binary = ''.join([bin(x)[2:].zfill(8) for x in octets])
            return str(binary.count('1'))
        except Exception:
            return "24"

class UBootConsole:
    def __init__(self, console):
        self.console = console

    def _send_command(self, cmd: str):
        if not cmd.endswith('\r\n'):
            cmd += '\r\n'
        self.console.send(cmd.encode('utf-8'))

    def _read_until(self,
                   target: str,
                   timeout: int = 60,
                   print_output: bool = False,
                   error_patterns: list[str] = None) -> str:
        start_time = time.time()
        buffer = ""
        error_patterns = error_patterns or ['error', 'failed']

        while (time.time() - start_time) < timeout:
            try:
                data = self.console.read_nonblocking(1024).decode('utf-8', errors='ignore')
                if data:
                    buffer += data
                    if print_output:
                        print(data, end="", flush=True)

                    if any(pattern in buffer.lower() for pattern in error_patterns):
                        raise RuntimeError(f"Error detected in output: {buffer}")

                    if target in buffer:
                        return buffer

            except Exception as e:
                if not isinstance(e, (RuntimeError, TimeoutError)):
                    time.sleep(0.1)
                else:
                    raise

        raise TimeoutError(f"Timed out waiting for '{target}'")

    def wait_for_uboot(self, timeout: int = 60):
        """Wait for U-Boot prompt"""
        print("Waiting for U-Boot prompt...")
        try:
            self._read_until("=>", timeout)
            return True
        except TimeoutError:
            return False

    def wait_for_pattern(self, pattern: str, timeout: int = 300, print_output: bool = False):
        """Wait for specific pattern in output"""
        return self._read_until(pattern, timeout, print_output)

    def get_env(self, var_name: str, timeout: int = 5) -> Optional[str]:
        """Get U-Boot environment variable value"""
        print(f"\nGetting U-Boot env var: {var_name}")
        self._send_command(f"printenv {var_name}")

        try:
            buffer = self._read_until("=>", timeout)
            for line in buffer.splitlines():
                if f"{var_name}=" in line:
                    return line.split('=', 1)[1].strip()
        except TimeoutError:
            raise TimeoutError(f"Timed out waiting for {var_name}")

        return None

    def set_env(self, key: str, value: str):
        cmd = f"setenv {key} '{value}'"
        print(f"Sending command: {cmd}")
        self._send_command(cmd)
        self._read_until("=>", timeout=5)

    def get_dhcp_info(self, timeout: int = 60) -> DhcpInfo:
        print("\nRunning DHCP to obtain network configuration...")
        self._send_command("dhcp")

        buffer = self._read_until("=>", timeout)

        # Extract IP and
        ip_address = None
        gateway = None

        for line in buffer.splitlines():
            if "DHCP client bound to address" in line:
                bind_index = line.find("DHCP client bound to address") + len("DHCP client bound to address")
                ip_end = line.find("(", bind_index)
                if ip_end != -1:
                    ip_address = line[bind_index:ip_end].strip()

            if "sending through gateway" in line:
                gw_index = line.find("sending through gateway") + len("sending through gateway")
                gateway = line[gw_index:].strip()

        if not ip_address or not gateway:
            raise ValueError("Could not extract complete network information")

        # Get netmask from environment
        netmask = self.get_env('netmask') or "255.255.255.0"

        return DhcpInfo(
            ip_address=ip_address,
            gateway=gateway,
            netmask=netmask
        )

    def tftp_boot(self,
                 load_address: str,
                 filename: str,
                 timeout: int = 300) -> bool:
        cmd = f"tftp {load_address} {filename}"
        self._send_command(cmd)
        try:
            self._read_until("=>", timeout)
            return True
        except TimeoutError:
            return False

    def run_command(self,
                   cmd: str,
                   timeout: int = 60,
                   wait_for_prompt: bool = True,
                   print_output: bool = False) -> Optional[str]:
        self._send_command(cmd)
        if wait_for_prompt:
            return self._read_until("=>", timeout, print_output)
        return None

    def boot(self, wait_for_prompt: bool = False, timeout: int = 60):
        return self.run_command("boot", timeout, wait_for_prompt)

    def interrupt_boot(self):
        self.console.send(b'\x03')  # CTRL+C
        time.sleep(0.1)
