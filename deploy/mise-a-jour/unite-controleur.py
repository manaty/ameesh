# SPDX-License-Identifier: AGPL-3.0-only
"""Fixed independent system unit; rendered into the signed bootstrap bundle."""
UNIT = '''# SPDX-License-Identifier: AGPL-3.0-only
[Unit]
Description=ameesh independent signed release controller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
Slice=system.slice
UMask=0077
Environment=PATH=/usr/bin:/bin LANG=C LC_ALL=C
ExecStart=/opt/ameesh-release/runtime/bin/python -I -B /opt/ameesh-release/lib/service-release.py
Restart=on-failure
RestartSec=10
KillMode=control-group
TimeoutStopSec=30
ReadOnlyPaths=/opt/ameesh-release/runtime /opt/ameesh-release/lib
PrivateTmp=no

[Install]
WantedBy=multi-user.target
'''
