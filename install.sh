#!/bin/sh
# Local source and standalone-binary installer. Never starts or enables p2afan.
set -eu

fail() { printf 'p2afan install: %s\n' "$*" >&2; exit 1; }
[ "$#" -eq 0 ] || fail 'usage: [DESTDIR=/absolute/staging/root] ./install.sh'
package=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
dest=${DESTDIR:-}
case "$dest" in
    '') ;;
    /*) [ "$dest" != / ] || fail 'DESTDIR=/ is not staging; omit DESTDIR for a live install' ;;
    *) fail 'DESTDIR must be an absolute path' ;;
esac
for file in bin/p2afan config/config.toml systemd/p2afan.service LICENSE README.md; do
    [ -f "$package/$file" ] || fail "package is missing $file"
done
kind=binary
[ ! -d "$package/p2afan" ] || kind=source

if [ -z "$dest" ]; then
    [ "$(id -u)" -eq 0 ] || fail 'live installation requires root'
    [ "$(uname -s)" = Linux ] || fail 'Linux is required'
    command -v systemctl >/dev/null 2>&1 || fail 'systemd/systemctl is required'
    service_status=0
    state=$(systemctl is-active p2afan.service) || service_status=$?
    case "$state:$service_status" in
        inactive:3|failed:3|unknown:4) ;;
        active:*|activating:*|deactivating:*|reloading:*) fail "service state is '$state'; stop p2afan.service before upgrading" ;;
        *) fail "cannot confirm p2afan.service is stopped (state '$state', status $service_status)" ;;
    esac
    if [ "$kind" = source ]; then
        command -v python3 >/dev/null 2>&1 || fail 'source installation requires Python 3.11+'
        python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || fail 'source installation requires Python 3.11+'
    else
        [ "$(uname -m)" = x86_64 ] || fail 'this standalone bundle requires Linux x86_64'
    fi
    command -v ipmitool >/dev/null 2>&1 || fail 'install ipmitool for BMC sensors, fan mapping and RPM checks'
fi

install -d -m 755 "$dest/opt/p2afan/bin" "$dest/usr/local/bin" "$dest/etc/p2afan" "$dest/etc/systemd/system"
if [ "$kind" = source ]; then
    install -d -m 755 "$dest/opt/p2afan/p2afan"
    install -m 644 "$package"/p2afan/*.py "$dest/opt/p2afan/p2afan/"
fi
install -m 755 "$package/bin/p2afan" "$dest/opt/p2afan/bin/p2afan"
install -m 755 "$package/bin/p2afan" "$dest/usr/local/bin/p2afan"
install -m 644 "$package/LICENSE" "$package/README.md" "$dest/opt/p2afan/"
if [ -d "$package/docs" ]; then
    install -d -m 755 "$dest/opt/p2afan/docs"
    install -m 644 "$package"/docs/*.md "$dest/opt/p2afan/docs/"
fi
install -m 644 "$package/systemd/p2afan.service" "$dest/etc/systemd/system/p2afan.service"
# Never create an active config or overwrite the operator's example/config/mapping.
if [ ! -e "$dest/etc/p2afan/config.toml" ] && [ ! -L "$dest/etc/p2afan/config.toml" ] \
    && [ ! -e "$dest/etc/p2afan/mapping.toml" ] && [ ! -L "$dest/etc/p2afan/mapping.toml" ] \
    && [ ! -e "$dest/etc/p2afan/config.toml.example" ] && [ ! -L "$dest/etc/p2afan/config.toml.example" ]; then
    install -m 644 "$package/config/config.toml" "$dest/etc/p2afan/config.toml.example"
fi
if [ -z "$dest" ]; then
    systemctl daemon-reload
fi
printf 'Installed %s payload. Service was NOT enabled or started.\n' "$kind"
printf 'Existing config.toml and mapping.toml are preserved.\n'
printf 'On a fresh install, copy /etc/p2afan/config.toml.example to config.toml, edit it, then run p2afan check-config.\n'
