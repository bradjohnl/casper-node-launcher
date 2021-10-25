#!/usr/bin/env python3
import ipaddress
import sys
from pathlib import Path
import urllib3
import argparse
import enum
import getpass
from ipaddress import ip_address
import tarfile
from collections import Counter
from shutil import chown
import os
import json


# protocol 1_0_0 should have accounts.toml
# All other protocols should have chainspec.toml, config.toml and NOT accounts.toml
# Protocols are shipped with config-example.toml to make config.toml


class Status(enum.Enum):
    UNSTAGED = 1
    NO_CONFIG = 2
    BIN_ONLY = 3
    CONFIG_ONLY = 4
    STAGED = 5
    WRONG_NETWORK = 6


class NodeUtil:
    """
    Using non `_` and non uppercase methods to expose for external commands.
    Description of command comes from the doc string of method.
    """
    CONFIG_PATH = Path("/etc/casper")
    BIN_PATH = Path("/var/lib/casper/bin")
    DB_PATH = Path("/var/lib/casper/casper-node")
    NET_CONFIG_PATH = CONFIG_PATH / "network_configs"
    PLATFORM_PATH = CONFIG_PATH / "PLATFORM"
    SCRIPT_NAME = "node_util.py"

    def __init__(self):
        self._network_name = None
        self._url = None
        self._bin_mode = None

        usage_docs = [f"{self.SCRIPT_NAME} <command> [args]", "Available commands:"]
        commands = []
        for function in [f for f in dir(self) if not f.startswith('_') and f[0].islower()]:
            try:
                usage_docs.append(f"  {function} - {getattr(self, function).__doc__.strip()}")
            except AttributeError:
                raise Exception(f"Error creating usage docs, expecting {function} to be root function and have doc comment."
                                f" Lead with underscore if not.")
            commands.append(function)
        usage_docs.append(" ")

        self._http = urllib3.PoolManager()
        self._external_ip = None

        parser = argparse.ArgumentParser(
            description="Utility to help configure casper-node versions and troubleshoot.",
            usage="\n".join(usage_docs))
        parser.add_argument("command", help="Subcommand to run.", choices=commands)
        args = parser.parse_args(sys.argv[1:2])
        getattr(self, args.command)()

    @staticmethod
    def _get_platform():
        if NodeUtil.PLATFORM_PATH.exists():
            return NodeUtil.PLATFORM_PATH.read_text().strip()
        else:
            return "deb"

    def _load_config_values(self, config):
        """
        Parses config file to get values

        :param file_name: network config filename
        """
        source_url = "SOURCE_URL"
        network_name = "NETWORK_NAME"
        bin_mode = "BIN_MODE"

        file_path = NodeUtil.NET_CONFIG_PATH / config
        expected_keys = (source_url, network_name)
        config = {}
        for line in file_path.read_text().splitlines():
            if line.strip():
                key, value = line.strip().split('=')
                config[key] = value
        for key in expected_keys:
            if key not in config.keys():
                print(f"Expected config value not found: {key} in {file_path}")
                exit(1)
        self._url = config[source_url]
        self._network_name = config[network_name]
        self._bin_mode = config.get(bin_mode, "mainnet")

    def _get_protocols(self):
        """ Downloads protocol versions for network """
        full_url = f"{self._network_url}/protocol_versions"
        r = self._http.request('GET', full_url)
        if r.status != 200:
            raise IOError(f"Expected status 200 requesting {full_url}, received {r.status}")
        pv = r.data.decode('utf-8')
        return [data.strip() for data in pv.splitlines()]

    @staticmethod
    def _verify_casper_user():
        if getpass.getuser() != "casper":
            print(f"Run with 'sudo -u casper'")
            exit(1)

    @staticmethod
    def _verify_root_user():
        if getpass.getuser() != "root":
            print("Run with 'sudo'")
            exit(1)

    @staticmethod
    def _status_text(status):
        status_display = {Status.UNSTAGED: "Protocol Unstaged",
                          Status.NO_CONFIG: "No config.toml for Protocol",
                          Status.BIN_ONLY: "Only bin is staged for Protocol, no config",
                          Status.CONFIG_ONLY: "Only config is staged for Protocol, no bin",
                          Status.WRONG_NETWORK: "chainspec.toml is for wrong network",
                          Status.STAGED: "Protocol Staged"}
        return status_display[status]

    def _check_staged_version(self, version):
        """
        Checks completeness of staged protocol version

        :param version: protocol version in underscore format such as 1_0_0
        :return: Status enum
        """
        if not self._network_name:
            print("Config not parsed prior to call of _check_staged_version and self._network_name is not populated.")
            exit(1)
        config_version_path = NodeUtil.CONFIG_PATH / version
        chainspec_toml_file_path = config_version_path / "chainspec.toml"
        config_toml_file_path = config_version_path / "config.toml"
        bin_version_path = NodeUtil.BIN_PATH / version / "casper-node"
        if not config_version_path.exists():
            if not bin_version_path.exists():
                return Status.UNSTAGED
            return Status.BIN_ONLY
        else:
            if not bin_version_path.exists():
                return Status.CONFIG_ONLY
            if not config_toml_file_path.exists():
                return Status.NO_CONFIG
            if NodeUtil._chainspec_name(chainspec_toml_file_path) != self._network_name:
                return Status.WRONG_NETWORK
        return Status.STAGED

    def _download_file(self, url, target_path):
        print(f"Downloading {url} to {target_path}")
        r = self._http.request('GET', url)
        if r.status != 200:
            raise IOError(f"Expected status 200 requesting {url}, received {r.status}")
        with open(target_path, 'wb') as f:
            f.write(r.data)

    @staticmethod
    def _extract_tar_gz(source_file_path, target_path):
        print(f"Extracting {source_file_path} to {target_path}")
        with tarfile.TarFile.open(source_file_path) as tf:
            for member in tf.getmembers():
                tf.extract(member, target_path)

    @property
    def _network_url(self):
        return f"http://{self._url}/{self._network_name}"

    def _pull_protocol_version(self, protocol_version, platform="deb"):
        self._verify_casper_user()

        if not NodeUtil.BIN_PATH.exists():
            print(f"Error: expected bin file location {NodeUtil.BIN_PATH} not found.")
            exit(1)

        if not NodeUtil.CONFIG_PATH.exists():
            print(f"Error: expected config file location {NodeUtil.CONFIG_PATH} not found.")
            exit(1)

        # Expectation is one config.tar.gz but multiple bin*.tar.gz
        # bin.tar.gz is mainnet bin and debian
        # bin_new.tar.gz is post 1.4.0 launch and debian
        # bin_rpm.tar.gz is mainnet bin and RHEL (_arch will be used for others in the future)
        # bin_rpm_new.tar.gz is post 1.4.0 launch and RHEL

        bin_file = "bin"
        if platform != "deb":
            # Handle alternative builds
            bin_file += f"_{platform}"
        if self._bin_mode != "mainnet":
            # Handle non mainnet for post 1.4.0 launched networks
            bin_file += "_new"
        bin_file += ".tar.gz"
        config_file = "config.tar.gz"
        print(f"Using bin mode file of {bin_file}")

        etc_full_path = NodeUtil.CONFIG_PATH / protocol_version
        bin_full_path = NodeUtil.BIN_PATH / protocol_version
        base_url = f"{self._network_url}/{protocol_version}"
        config_url = f"{base_url}/{config_file}"
        bin_url = f"{base_url}/{bin_file}"

        if etc_full_path.exists():
            print(f"Error: config version path {etc_full_path} already exists. Aborting.")
            exit(1)
        if bin_full_path.exists():
            print(f"Error: bin version path {bin_full_path} already exists. Aborting.")
            exit(1)

        config_archive_path = NodeUtil.CONFIG_PATH / config_file
        self._download_file(config_url, config_archive_path)
        self._extract_tar_gz(config_archive_path, etc_full_path)
        print(f"Deleting {config_archive_path}")
        config_archive_path.unlink()

        bin_archive_path = NodeUtil.BIN_PATH / bin_file
        self._download_file(bin_url, bin_archive_path)
        self._extract_tar_gz(bin_archive_path, bin_full_path)
        print(f"Deleting {bin_archive_path}")
        bin_archive_path.unlink()

    def _get_external_ip(self):
        if self._external_ip:
            return self._external_ip
        services = (("https://checkip.amazonaws.com", "amazonaws.com"),
                    ("https://ifconfig.me", "ifconfig.me"),
                    ("https://ident.me", "ident.me"))
        ips = []
        # Using our own PoolManager for shorter timeouts
        http = urllib3.PoolManager(timeout=10)
        print("Querying your external IP...")
        for url, service in services:
            r = http.request('GET', url, )
            if r.status != 200:
                ip = ""
            else:
                ip = r.data.decode('utf-8').strip()
            print(f" {service} says '{ip}' with Status: {r.status}")
            if ip:
                ips.append(ip)
        if ips:
            ip_addr = Counter(ips).most_common(1)[0][0]
            if self._is_valid_ip(ip_addr):
                self._external_ip = ip_addr
                return ip_addr
        return None

    @staticmethod
    def _is_valid_ip(ip):
        try:
            _ = ipaddress.ip_address(ip)
        except ValueError:
            return False
        else:
            return True

    def _config_from_example(self, protocol_version, ip=None):
        """ Create config.toml or config.toml.new (if previous exists) from config-example.toml"""
        self._verify_casper_user()

        config_path = NodeUtil.CONFIG_PATH / protocol_version
        config_toml_path = config_path / "config.toml"
        config_example = config_path / "config-example.toml"
        config_toml_new_path = config_path / "config.toml.new"

        if not config_example.exists():
            print(f"Error: {config_example} not found.")
            exit(1)

        if ip is None:
            ip = self._get_external_ip()
            print(f"Using detected ip: {ip}")
        else:
            print(f"Using provided ip: {ip}")

        if not self._is_valid_ip(ip):
            print(f"Error: Invalid IP: {ip}")
            exit(1)

        outfile = config_toml_path
        if config_toml_path.exists():
            outfile = config_toml_new_path
            print(f"Previous {config_toml_path} exists, creating as {outfile} from {config_example}.")
            print(f"Replace {config_toml_path} with {outfile} to use the automatically generated configuration.")

        outfile.write_text(config_example.read_text().replace("<IP ADDRESS>", ip))

    def stage_protocols(self):
        """Stage available protocols if needed (use 'sudo -u casper')"""
        parser = argparse.ArgumentParser(description=self.stage_protocols.__doc__,
                                         usage=f"{self.SCRIPT_NAME} stage_protocols [-h] config [--ip IP]")
        parser.add_argument("config", type=str, help=f"name of config file to use from {NodeUtil.NET_CONFIG_PATH}")
        parser.add_argument("--ip",
                            type=ip_address,
                            help=f"optional ip to use for config.toml instead of detected ip.",
                            required=False)
        args = parser.parse_args(sys.argv[2:])
        self._load_config_values(args.config)

        self._verify_casper_user()
        platform = self._get_platform()
        exit_code = 0
        for pv in self._get_protocols():
            status = self._check_staged_version(pv)
            if status == Status.STAGED:
                print(f"{pv}: {self._status_text(status)}")
                continue
            elif status in (Status.BIN_ONLY, Status.CONFIG_ONLY):
                print(f"{pv}: {self._status_text(status)} - Not automatically recoverable.")
                exit_code = 1
                continue
            if status == Status.UNSTAGED:
                print(f"Pulling protocol for {pv}.")
                if not self._pull_protocol_version(pv, platform):
                    exit_code = 1
            if status in (Status.UNSTAGED, Status.NO_CONFIG):
                print(f"Creating config for {pv}.")
                ip = str(args.ip) if args.ip else None
                if not self._config_from_example(pv, ip):
                    exit_code = 1
        exit(exit_code)

    def check_protocols(self):
        """ Checks if protocol are fully installed """
        parser = argparse.ArgumentParser(description=self.check_protocols.__doc__,
                                         usage=f"{self.SCRIPT_NAME} check_protocols [-h] config ")
        parser.add_argument("config", type=str, help=f"name of config file to use from {NodeUtil.NET_CONFIG_PATH}")
        args = parser.parse_args(sys.argv[2:])
        self._load_config_values(args.config)

        exit_code = 0
        for pv in self._get_protocols():
            status = self._check_staged_version(pv)
            if status != Status.STAGED:
                exit_code = 1
            print(f"{pv}: {self._status_text(status)}")
        exit(exit_code)

    def check_for_upgrade(self):
        """ Checks if last protocol is staged """
        parser = argparse.ArgumentParser(description=self.check_for_upgrade.__doc__,
                                         usage=f"{self.SCRIPT_NAME} check_for_upgrade [-h] config ")
        parser.add_argument("config", type=str, help=f"name of config file to use from {NodeUtil.NET_CONFIG_PATH}")
        args = parser.parse_args(sys.argv[2:])
        self._load_config_values(args.config)
        last_protocol = self._get_protocols()[-1]
        status = self._check_staged_version(last_protocol)
        if status == Status.UNSTAGED:
            print(f"{last_protocol}: {self._status_text(status)}")
            exit(1)
        exit(0)

    @staticmethod
    def _is_casper_owned(path) -> bool:
        return path.owner() == 'casper' and path.group() == 'casper'

    @staticmethod
    def _walk_file_locations():
        for path in NodeUtil.BIN_PATH, NodeUtil.CONFIG_PATH, NodeUtil.DB_PATH:
            for _path in NodeUtil._walk_path(path):
                yield _path

    @staticmethod
    def _walk_path(path, include_dir=True):
        for p in Path(path).iterdir():
            if p.is_dir():
                if include_dir:
                    yield p.resolve()
                yield from NodeUtil._walk_path(p)
                continue
            yield p.resolve()

    def check_permissions(self):
        """ Checking files are owned by casper. """
        # If a user runs commands under root, it can give files non casper ownership and cause problems.
        exit_code = 0
        for path in self._walk_file_locations():
            if not self._is_casper_owned(path):
                print(f"{path} is owned by {path.owner()}:{path.group()}")
                exit_code = 1
        if exit_code == 0:
            print("Permissions are correct.")
        exit(exit_code)

    def fix_permissions(self):
        """ Sets all files owner to casper (use 'sudo') """
        self._verify_root_user()

        exit_code = 0
        for path in self._walk_file_locations():
            if not self._is_casper_owned(path):
                print(f"Correcting ownership of {path}")
                chown(path, 'casper', 'casper')
                if not self._is_casper_owned(path):
                    print(f"Ownership set failed.")
                    exit_code = 1
        exit(exit_code)

    def rotate_logs(self):
        """ Rotate the logs for casper-node (use 'sudo') """
        self._verify_root_user()
        os.system("logrotate -f /etc/logrotate.d/casper-node")

    def delete_local_state(self):
        """ Delete local db and status files. (use 'sudo') """
        parser = argparse.ArgumentParser(description=self.delete_local_state.__doc__,
                                         usage=f"{self.SCRIPT_NAME} delete_local_state [-h] --verify-delete-all")
        parser.add_argument("--verify_delete_all",
                            action='store_true',
                            help="Required for verification that you want to delete everything",
                            required=False)
        args = parser.parse_args(sys.argv[2:])
        self._verify_root_user()

        if not args.verify_delete_all:
            print(f"Include '--verify_delete_all' flag to confirm. Exiting.")
            exit(1)

        # missing_ok=True arg to unlink only 3.8+, using try/catch.
        for path in self.DB_PATH.glob('*'):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        cnl_state = self.CONFIG_PATH / "casper-node-launcher-state.toml"
        try:
            cnl_state.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _ip_address_type(ip_address: str):
        try:
            ip = ipaddress.ip_address(ip_address)
        except ValueError:
            print(f"Error: Invalid IP: {ip_address}")
        else:
            return str(ip)

    def _get_status(self, ip, port):
        """ Get status data from node """
        full_url = f"{ip}:{port}/status"
        r = self._http.request('GET', full_url)
        if r.status != 200:
            raise IOError(f"Expected status 200 requesting {full_url}, received {r.status}")
        return json.loads(r.data.decode('utf-8'))

    @staticmethod
    def _chainspec_name(chainspec_path) -> str:
        # Hack to not require toml package install
        for line in chainspec_path.read_text().splitlines():
            NAME_DATA = "name = '"
            if line[:len(NAME_DATA)] == NAME_DATA:
                return line.split(NAME_DATA)[1].split("'")[0]


NodeUtil()