# Lingling

OpenCode gives away good models for free. the catch is a daily limit -- it can
follow an IP, a request size, or one session record. a new session sometimes
keeps working when an old one is stuck.

Lingling spreads requests across the `/64`s of your own Route64 tunnel. it
moves a lane at half the day's allowance, before the wall, so one bucket never
stops the session. OpenCode keeps the same local window the whole time.

## Needs

- Windows
- a free [Route64](https://manager.route64.org) account
- [WireGuard](https://www.wireguard.com/install/) -- setup installs it if missing
- `opencode` on your PATH
- Python 3.11 or newer

## Install

```bash
pip install lingling
lingling
```

first launch runs setup.

## Setup

1. sign in at [manager.route64.org](https://manager.route64.org).
2. open **IPv6 Tunnelbroker** and create a **WireGuard** tunnel.
3. download or copy the WireGuard config.
4. open **List IP subnets** and copy the routed `/56`.

run `lingling` and paste the config path when asked. drag and drop works.

the address inside the config is the tunnel `/64`:

```text
Address = 2001:db8:1:2::2/64
```

its not the block Lingling rotates through. use the routed `/56` that Route64
shows separately, for example:

```text
2001:db8:1:300::/56
```

setup installs a WireGuard service called `lingling`, asks for admin once, and
starts the service only while Lingling is open. to replace the profile:

```bash
lingling setup --reset
```

## Use

```bash
lingling
```

Lingling starts the tunnel, picks the first unused `/64`, and opens OpenCode
with its own local proxy. it spreads each lane to half the daily allowance
before moving. a free-limit wall spends the walled lane and moves to a fresh
one. OpenCode can mark one session as used while a new session still works, so
Lingling opens a fresh root session with the current chat context first. if
that fresh session is rejected too, it falls back to a summary handoff. the old
chat stays as a backup; the new session opens in the same folder with the model
and effort you picked. spent lanes reset at `00:00 UTC`.

the proof log sits in the data folder -- `%TEMP%\lingling-data\route.log`
by default.

every path, name, timing, and color can be overridden with a `LINGLING_*`
environment variable; defaults live in `lingling/settings.py`.

## Commands

```text
lingling                 start a session
lingling setup           set up or replace your Route64 profile
lingling setup --reset   remove the old profile and run setup again
lingling update          install the newest lingling
lingling uninstall       remove the profile and tunnel
```

## License

see [LICENSE](LICENSE).

## apology

yeah i know. this update took way longer than it should have. no excuses -- i
just kept finding shit to fix and the whole thing dragged. sorry to all the ppl
who kept checking back or thought the project was dead. it wasnt. 2.2.0 is out
now and i'll try to be faster next time. if u waited, thanks for sticking
around.
