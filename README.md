# Lingling

OpenCode gives away good models for free. The catch is a daily limit -- it can
follow an IP, a request size, or one session record. A new session sometimes
keeps working when an old one is stuck.

Lingling spreads requests across the `/64`s of your own Route64 tunnel. It
moves a lane at half the day's allowance, before the wall, so one bucket never
stops the session. OpenCode keeps the same local window the whole time.

## Needs

- Windows
- A free [Route64](https://manager.route64.org) account
- [WireGuard](https://www.wireguard.com/install/) -- setup installs it if missing
- `opencode` on your PATH
- Python 3.11 or newer

## Install

```bash
pip install lingling
lingling
```

The first launch runs setup.

## Setup

1. Sign in at [manager.route64.org](https://manager.route64.org).
2. Open **IPv6 Tunnelbroker** and create a **WireGuard** tunnel.
3. Download or copy the WireGuard config.
4. Open **List IP subnets** and copy the routed `/56`.

Run `lingling` and paste the config path when asked. Drag and drop works.

The address inside the config is the tunnel `/64`:

```text
Address = 2001:db8:1:2::2/64
```

It is not the block Lingling rotates through. Use the routed `/56` that
Route64 shows separately, for example:

```text
2001:db8:1:300::/56
```

Setup installs a WireGuard service called `lingling`, asks for admin once,
and starts the service only while Lingling is open. To replace the profile:

```bash
lingling setup --reset
```

## Use

```bash
lingling
```

Lingling starts the tunnel, picks the first unused `/64`, and opens OpenCode
with its own local proxy. It spreads each lane to half the daily allowance
before moving. A free-limit wall spends the walled lane and moves to a fresh
one. OpenCode can mark one session as used while a new session still works, so
Lingling opens a fresh root session with the current chat context first. If
that fresh session is rejected too, it falls back to a summary handoff. The old
chat stays as a backup; the new session opens in the same folder with the model
and effort you picked. Spent lanes reset at `00:00 UTC`.

The proof log sits in the data folder -- `%TEMP%\lingling-data\route.log`
by default.

Every path, name, timing, and color can be overridden with a `LINGLING_*`
environment variable; the defaults live in `lingling/settings.py`.

## Commands

```text
lingling                 start a session
lingling setup           set up or replace your Route64 profile
lingling setup --reset   remove the old profile and run setup again
lingling update          install the newest lingling
lingling uninstall       remove the profile and tunnel
```

## License

See [LICENSE](LICENSE).
