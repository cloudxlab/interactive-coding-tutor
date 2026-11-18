# Baked into the per-user image. Everything is overridable by environment variables for local testing.
import os

c.ServerApp.ip = "0.0.0.0"
c.ServerApp.port = 8888
c.ServerApp.port_retries = 0
c.ServerApp.open_browser = False
c.ServerApp.base_url = "/jupyter/"
c.ServerApp.root_dir = os.environ.get("TUTOR_ROOT", "/work")
# The container is only reachable through the app's authenticated proxy (which injects the token),
# so Jupyter's own Host/Origin checks, which see the proxy rather than the browser, are relaxed.
c.ServerApp.allow_origin = "*"
c.ServerApp.allow_remote_access = True
c.ServerApp.trust_xheaders = True
c.ServerApp.jpserver_extensions = {"jupyter_server_terminals": False}  # no web terminal
c.ServerApp.tornado_settings = {"headers": {"Content-Security-Policy": "frame-ancestors 'self'"}}
c.LabApp.app_settings_dir = os.environ.get("TUTOR_JLAB_SETTINGS", "/opt/tutor/jlab_settings")
