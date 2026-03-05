"""IB Client Portal Gateway lifecycle management."""

import logging
import os
import shutil
import signal
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path

import requests

from .config import load_config, save_config, get_account_config, get_all_account_names
from .http_client import IBHttpClient

logger = logging.getLogger("ib-connect")

GATEWAY_DOWNLOAD_URL = "https://download2.interactivebrokers.com/portal/clientportal.gw.zip"


class GatewayManager:
    """Manages IB Client Portal Gateway instances for multiple accounts."""

    def __init__(self, cfg: dict, http: IBHttpClient):
        self.cfg = cfg
        self.http = http
        self._pids_dir = Path(cfg.get("gateway_jar_path", "~/.ib-connect/gateway")).expanduser().parent / "pids"
        self._pids_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir = Path(cfg.get("log_dir", "~/.ib-connect/logs")).expanduser()
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._gateway_base = Path(cfg.get("gateway_jar_path", "~/.ib-connect/gateway")).expanduser()
        self._java_path = cfg.get("java_path", "/opt/homebrew/opt/openjdk/bin/java")

        # Validate stale PIDs on init
        self._cleanup_stale_pids()

    def _cleanup_stale_pids(self):
        """Remove PID files for processes that are no longer running."""
        for pid_file in self._pids_dir.glob("*.pid"):
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)  # Check if process exists
            except (ProcessLookupError, ValueError, OSError):
                account = pid_file.stem
                logger.info("Removing stale PID file for %s (pid %s)", account, pid_file.read_text().strip())
                pid_file.unlink()

    # --- Java dependency ---

    def check_java(self) -> dict:
        """Check if Java is available. Returns status dict."""
        try:
            result = subprocess.run(
                [self._java_path, "-version"],
                capture_output=True, text=True, timeout=10
            )
            version_line = result.stderr.split("\n")[0] if result.stderr else result.stdout.split("\n")[0]
            return {"available": True, "version": version_line}
        except FileNotFoundError:
            return {
                "available": False,
                "message": "Java not found. Install via: brew install openjdk"
            }
        except Exception as e:
            return {"available": False, "message": f"Java check failed: {e}"}

    # --- Gateway download ---

    def gateway_exists(self) -> bool:
        """Check if gateway files are present."""
        run_script = self._gateway_base / "bin" / "run.sh"
        return run_script.exists()

    def download_gateway(self) -> dict:
        """Download and extract IB Client Portal Gateway."""
        logger.info("Downloading IB Client Portal Gateway...")
        try:
            self._gateway_base.mkdir(parents=True, exist_ok=True)
            zip_path = self._gateway_base.parent / "clientportal.gw.zip"

            resp = requests.get(GATEWAY_DOWNLOAD_URL, timeout=60, stream=True)
            resp.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            # Extract to gateway directory
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(self._gateway_base)

            zip_path.unlink()

            # The zip may contain a nested directory; find run.sh
            run_script = self._find_run_script(self._gateway_base)
            if run_script:
                # If nested, move contents up
                nested_dir = run_script.parent.parent
                if nested_dir != self._gateway_base:
                    for item in nested_dir.iterdir():
                        dest = self._gateway_base / item.name
                        if dest.exists():
                            if dest.is_dir():
                                shutil.rmtree(dest)
                            else:
                                dest.unlink()
                        shutil.move(str(item), str(dest))

            # Make run.sh executable
            run_sh = self._gateway_base / "bin" / "run.sh"
            if run_sh.exists():
                run_sh.chmod(run_sh.stat().st_mode | 0o755)

            logger.info("Gateway downloaded and extracted to %s", self._gateway_base)
            return {"success": True, "path": str(self._gateway_base)}

        except Exception as e:
            logger.error("Gateway download failed: %s", e)
            return {"success": False, "error": str(e)}

    def _find_run_script(self, base: Path) -> Path | None:
        """Find bin/run.sh recursively."""
        for p in base.rglob("run.sh"):
            if p.parent.name == "bin":
                return p
        return None

    # --- Per-account gateway directory ---

    def _account_gateway_dir(self, account: str) -> Path:
        return self._gateway_base.parent / f"gateway-{account}"

    @staticmethod
    def _detect_system_country() -> str:
        """Detect country code from macOS locale settings."""
        try:
            result = subprocess.run(
                ["defaults", "read", ".GlobalPreferences", "AppleLocale"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                locale = result.stdout.strip()  # e.g. "en_DE", "de_CH"
                if "_" in locale:
                    return locale.split("_")[1][:2].upper()
        except Exception:
            pass
        return "US"  # fallback

    def _prepare_account_gateway(self, account: str, port: int):
        """Copy gateway to account-specific directory and configure port + locale."""
        acct_dir = self._account_gateway_dir(account)

        if acct_dir.exists():
            shutil.rmtree(acct_dir)
        shutil.copytree(self._gateway_base, acct_dir)

        # Update port and ip2loc in conf.yaml
        conf_path = acct_dir / "root" / "conf.yaml"
        if conf_path.exists():
            import re
            content = conf_path.read_text()
            content = re.sub(
                r'(listenPort:\s*)\d+',
                f'\\g<1>{port}',
                content
            )
            country = self._detect_system_country()
            content = re.sub(
                r'(ip2loc:\s*)"[^"]*"',
                f'\\g<1>"{country}"',
                content
            )
            conf_path.write_text(content)
            logger.info("Configured gateway for %s on port %d, ip2loc=%s", account, port, country)
        else:
            logger.warning("conf.yaml not found at %s", conf_path)

        # Make run.sh executable
        run_sh = acct_dir / "bin" / "run.sh"
        if run_sh.exists():
            run_sh.chmod(run_sh.stat().st_mode | 0o755)

    # --- Gateway process lifecycle ---

    def _pid_file(self, account: str) -> Path:
        return self._pids_dir / f"{account}.pid"

    def _get_pid(self, account: str) -> int | None:
        pf = self._pid_file(account)
        if pf.exists():
            try:
                pid = int(pf.read_text().strip())
                os.kill(pid, 0)
                return pid
            except (ProcessLookupError, ValueError, OSError):
                pf.unlink()
        return None

    def is_running(self, account: str) -> bool:
        return self._get_pid(account) is not None

    def start(self, account: str) -> dict:
        """Start gateway for a single account."""
        if self.is_running(account):
            acct_cfg = get_account_config(self.cfg, account)
            return {
                "status": "already_running",
                "account": account,
                "port": acct_cfg["port"]
            }

        # Check Java
        java_status = self.check_java()
        if not java_status["available"]:
            return {"status": "error", "error": java_status["message"]}

        # Check/download gateway
        if not self.gateway_exists():
            dl = self.download_gateway()
            if not dl["success"]:
                return {"status": "error", "error": f"Gateway download failed: {dl['error']}"}

        acct_cfg = get_account_config(self.cfg, account)
        port = acct_cfg["port"]

        # Prepare account-specific gateway directory
        self._prepare_account_gateway(account, port)

        acct_dir = self._account_gateway_dir(account)
        run_script = acct_dir / "bin" / "run.sh"

        if not run_script.exists():
            return {"status": "error", "error": f"run.sh not found at {run_script}"}

        # Start gateway process
        log_path = self._log_dir / f"gateway-{account}.log"
        self._rotate_log(log_path)

        env = os.environ.copy()
        env["JAVA_HOME"] = str(Path(self._java_path).parent.parent)
        env["PATH"] = str(Path(self._java_path).parent) + ":" + env.get("PATH", "")

        conf_yaml = "../root/conf.yaml"
        classpath = ":".join([
            "root",
            "dist/ibgroup.web.core.iblink.router.clientportal.gw.jar",
            "build/lib/runtime/*",
        ])
        log_file = open(log_path, "a")
        process = subprocess.Popen(
            [
                self._java_path,
                "-server",
                "-Dvertx.disableDnsResolver=true",
                "-Djava.net.preferIPv4Stack=true",
                "-Dvertx.logger-delegate-factory-class-name=io.vertx.core.logging.SLF4JLogDelegateFactory",
                "-Dnologback.statusListenerClass=ch.qos.logback.core.status.OnConsoleStatusListener",
                "-Dnolog4j.debug=true",
                "-Dnolog4j2.debug=true",
                "-cp", classpath,
                "ibgroup.web.core.clientportal.gw.GatewayStart",
                "--conf", conf_yaml,
            ],
            cwd=str(acct_dir),
            stdout=log_file,
            stderr=log_file,
            env=env,
            preexec_fn=os.setpgrp
        )

        # Save PID
        self._pid_file(account).write_text(str(process.pid))
        logger.info("Started gateway for %s (pid %d) on port %d", account, process.pid, port)

        # Brief wait to confirm the process didn't crash on startup.
        # Don't wait for the health endpoint — it returns 401 until the
        # user authenticates, so a long wait here just delays login page opening.
        time.sleep(3)
        try:
            os.kill(process.pid, 0)
        except OSError:
            last_lines = self._tail_log(log_path, 50)
            return {
                "status": "error",
                "error": f"Gateway for {account} crashed during startup.",
                "log_tail": last_lines
            }

        return {
            "status": "started",
            "account": account,
            "port": port,
            "pid": process.pid,
            "url": f"https://localhost:{port}"
        }

    def _wait_for_ready(self, port: int, max_wait: int = 30) -> bool:
        """Poll health endpoint until gateway responds."""
        start = time.time()
        while time.time() - start < max_wait:
            if self.http.health_check(port, timeout=3):
                return True
            time.sleep(2)
        return False

    def stop(self, account: str) -> dict:
        """Stop gateway for a single account."""
        pid = self._get_pid(account)
        if pid is None:
            return {"status": "not_running", "account": account}

        try:
            os.kill(pid, signal.SIGTERM)
            # Wait briefly for clean shutdown
            for _ in range(10):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.5)
                except ProcessLookupError:
                    break
            else:
                # Force kill if still alive
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except ProcessLookupError:
            pass

        self._pid_file(account).unlink(missing_ok=True)
        logger.info("Stopped gateway for %s (pid %d)", account, pid)
        return {"status": "stopped", "account": account, "pid": pid}

    def open_login_page(self, account: str) -> str:
        """Open gateway login page in browser."""
        acct_cfg = get_account_config(self.cfg, account)
        port = acct_cfg["port"]
        url = f"https://localhost:{port}"
        browser_cmd = self.cfg.get("browser_command", "open")
        try:
            subprocess.Popen([browser_cmd, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("Opened login page for %s at %s", account, url)
        except Exception as e:
            logger.error("Failed to open browser for %s: %s", account, e)
        return url

    def wait_for_auth(self, account: str, timeout: int = 120) -> bool:
        """Poll auth status until authenticated or timeout."""
        acct_cfg = get_account_config(self.cfg, account)
        port = acct_cfg["port"]
        start = time.time()
        while time.time() - start < timeout:
            status = self.http.auth_status(port)
            if status.get("authenticated", False):
                # Initialize brokerage session
                self.http.init_brokerage_session(port)
                return True
            time.sleep(3)
        return False

    def get_status(self, account: str) -> dict:
        """Get detailed status for a single account."""
        acct_cfg = get_account_config(self.cfg, account)
        port = acct_cfg["port"]
        running = self.is_running(account)

        result = {
            "gateway_running": running,
            "authenticated": False,
            "connected": False,
            "account_id": acct_cfg.get("account_id", ""),
            "label": acct_cfg.get("label", account),
            "port": port
        }

        if running:
            auth = self.http.auth_status(port)
            result["authenticated"] = auth.get("authenticated", False)
            result["connected"] = auth.get("connected", False) or result["authenticated"]

        return result

    def tickle_all(self):
        """Tickle all running gateways to keep sessions alive."""
        for account in get_all_account_names(self.cfg):
            if self.is_running(account):
                acct_cfg = get_account_config(self.cfg, account)
                self.http.tickle(acct_cfg["port"])

    def auto_discover_account_id(self, account: str) -> str | None:
        """Discover IB account ID after first successful auth."""
        acct_cfg = get_account_config(self.cfg, account)
        if acct_cfg.get("account_id"):
            return acct_cfg["account_id"]

        port = acct_cfg["port"]
        try:
            resp = self.http.get(
                f"https://localhost:{port}/v1/api/portfolio/accounts",
                rate_limit=False
            )
            if resp.status_code == 200:
                accounts = resp.json()
                if accounts and len(accounts) > 0:
                    account_id = accounts[0].get("id", accounts[0].get("accountId", ""))
                    if account_id:
                        # Write back to config
                        cfg = load_config()
                        cfg["accounts"][account]["account_id"] = account_id
                        save_config(cfg)
                        self.cfg = cfg
                        logger.info("Auto-discovered account_id for %s: %s", account, account_id)
                        return account_id
        except Exception as e:
            logger.error("Account ID auto-discovery failed for %s: %s", account, e)
        return None

    # --- Gateway update ---

    def check_for_update(self) -> dict:
        """Check if a newer gateway version is available. Max once per 24h."""
        last_check = self.cfg.get("last_update_check")
        if last_check:
            try:
                last_dt = datetime.fromisoformat(last_check)
                hours_since = (datetime.now() - last_dt).total_seconds() / 3600
                if hours_since < 24:
                    return {"checked": False, "reason": "checked_recently", "hours_since": hours_since}
            except (ValueError, TypeError):
                pass

        try:
            resp = requests.head(GATEWAY_DOWNLOAD_URL, timeout=10)
            remote_modified = resp.headers.get("Last-Modified")

            # Compare against local gateway modification date
            run_sh = self._gateway_base / "bin" / "run.sh"
            if run_sh.exists() and remote_modified:
                from email.utils import parsedate_to_datetime
                remote_dt = parsedate_to_datetime(remote_modified)
                local_dt = datetime.fromtimestamp(run_sh.stat().st_mtime, tz=remote_dt.tzinfo)

                update_available = remote_dt > local_dt

                cfg = load_config()
                cfg["last_update_check"] = datetime.now().isoformat()
                cfg["update_available"] = update_available
                save_config(cfg)
                self.cfg = cfg

                return {
                    "checked": True,
                    "update_available": update_available,
                    "local_date": local_dt.isoformat(),
                    "remote_date": remote_dt.isoformat()
                }

            cfg = load_config()
            cfg["last_update_check"] = datetime.now().isoformat()
            save_config(cfg)
            self.cfg = cfg
            return {"checked": True, "update_available": False}

        except Exception as e:
            logger.warning("Gateway update check failed: %s", e)
            return {"checked": False, "reason": f"check_failed: {e}"}

    def apply_update(self) -> dict:
        """Download and apply gateway update with rollback support."""
        backup_dir = self._gateway_base.parent / "gateway.backup"

        # 1. Stop all running gateways
        for account in get_all_account_names(self.cfg):
            if self.is_running(account):
                self.stop(account)

        # 2. Backup current gateway
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        if self._gateway_base.exists():
            shutil.copytree(self._gateway_base, backup_dir)

        # 3. Delete current gateway and account copies
        if self._gateway_base.exists():
            shutil.rmtree(self._gateway_base)
        for account in get_all_account_names(self.cfg):
            acct_dir = self._account_gateway_dir(account)
            if acct_dir.exists():
                shutil.rmtree(acct_dir)

        # 4. Download new version
        dl = self.download_gateway()
        if not dl["success"]:
            # Rollback
            return self._rollback(backup_dir, f"Download failed: {dl['error']}")

        # 5. Restart gateways
        for account in get_all_account_names(self.cfg):
            result = self.start(account)
            if result["status"] == "error":
                return self._rollback(backup_dir, f"Gateway start failed for {account}: {result['error']}")

        # 6. Update config
        cfg = load_config()
        cfg["last_update_check"] = datetime.now().isoformat()
        cfg["update_available"] = False
        cfg["rollback_active"] = False
        cfg["rollback_reason"] = None
        save_config(cfg)
        self.cfg = cfg

        return {"success": True, "message": "Gateway updated. Re-authentication required for all accounts."}

    def _rollback(self, backup_dir: Path, reason: str) -> dict:
        """Restore gateway from backup."""
        logger.error("Gateway update failed, rolling back: %s", reason)

        if self._gateway_base.exists():
            shutil.rmtree(self._gateway_base)
        if backup_dir.exists():
            shutil.copytree(backup_dir, self._gateway_base)

        cfg = load_config()
        cfg["rollback_active"] = True
        cfg["rollback_reason"] = reason
        save_config(cfg)
        self.cfg = cfg

        # Try to restart with old version
        for account in get_all_account_names(self.cfg):
            self.start(account)

        return {"success": False, "error": reason, "rollback": True}

    # --- Helpers ---

    def _rotate_log(self, log_path: Path):
        """Rotate log file if it exceeds 10MB."""
        if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
            rotated = log_path.with_suffix(".log.1")
            if rotated.exists():
                rotated.unlink()
            log_path.rename(rotated)

    def _tail_log(self, log_path: Path, lines: int = 50) -> str:
        """Return last N lines of a log file."""
        if not log_path.exists():
            return "(log file not found)"
        try:
            with open(log_path, "r") as f:
                all_lines = f.readlines()
                return "".join(all_lines[-lines:])
        except Exception:
            return "(could not read log file)"

    def gateway_version_date(self) -> str | None:
        """Get modification date of current gateway installation."""
        run_sh = self._gateway_base / "bin" / "run.sh"
        if run_sh.exists():
            mtime = run_sh.stat().st_mtime
            return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        return None
