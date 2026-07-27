# skyhub-allsky
SkyHub is an open-source Allsky application designed to be modular and modern with a flexible server-client structure.

## Run modes

SkyHub is designed to run in two shapes:

- Standalone: one Pi or machine runs the server and camera node together.
- Split: a house server runs the server, and one or more outside Pis run camera nodes.

The root launcher keeps those modes consistent:

```bash
python skyhub.py server
python skyhub.py node
python skyhub.py standalone
```

Examples:

```bash
python skyhub.py standalone --node-id roof-pi
python skyhub.py server --port 8000
python skyhub.py node --node-id roof-pi --server-ws-base-url ws://skyhub.local:8000/ws/nodes
python skyhub.py node --node-id pi5-hqcam --camera-driver picamera2 --server-ws-base-url ws://skyhub.local:8000/ws/nodes
```

Standalone is not a separate app. It starts the existing server and node processes together on one machine.

## Raspberry Pi node

Clone SkyHub on the Pi first:

```bash
git clone <your-skyhub-repo-url> skyhub
cd skyhub
```

Then install the Pi camera package and run the node:

```bash
bash scripts/install-node.sh
python3 skyhub.py node
```

The node uses the mock camera by default. Use `--camera-driver picamera2` for a Raspberry Pi camera.

The installer writes `node/.env`. For non-interactive setup, pass values as environment variables:

```bash
SKYHUB_NODE_NODE_ID=pi5-hqcam \
SKYHUB_NODE_SERVER_WS_BASE_URL=ws://WINDOWS_IP:8000/ws/nodes \
SKYHUB_NODE_CAMERA_DRIVER=picamera2 \
bash scripts/install-node.sh
```

## Signing in

The web UI has one admin account, protected by a password and a TOTP code from an
authenticator app. On first start the server prints a one-time setup token:

```
auth.setup.required  setup_token=WA-_fwzE3KPaCbFXc1xcgxc417yMHNbw
```

Open the UI, enter that token, pick a username and password, scan the QR code with
Google Authenticator, Aegis, 1Password, Bitwarden or anything else that does TOTP,
and confirm with a six-digit code. The token is also written to
`data/setup-token.txt`, and both copies are destroyed once setup finishes.

Passwords are stored as Argon2id hashes, sessions live on the server behind an
HttpOnly cookie, and Settings → Security is where you change the password, replace
the authenticator, review active sessions and sign out. Full details, including how
to recover a lost authenticator, are in [docs/AUTH.md](docs/AUTH.md).

## API

Everything the web UI does is available over HTTP, so the server can drive Home
Assistant, Node-RED or your own scripts. See [docs/API.md](docs/API.md) for the
full reference, or browse the live schema at `http://<server>:8000/docs`.

Camera nodes and scripts do not log in - they cannot answer a TOTP prompt - so they
keep using the shared API key. Set one on the server and give the same value to each
node:

```bash
SKYHUB_SERVER_API_KEY=pick-something-long-and-random python skyhub.py server
SKYHUB_NODE_API_KEY=pick-something-long-and-random python skyhub.py node
```

Without a key set, your own login still works, but any machine that can reach the
port can connect as a node and upload captures - fine on a trusted LAN, not for
anything reachable from the internet. The key never opens the account itself: it
cannot change your password, replace your authenticator or read your sessions.

To publish the latest image somewhere without handing out that key - which can also
change settings and stop capture - open up the current image on its own:

```bash
SKYHUB_SERVER_PUBLIC_CAPTURE_TOKEN=some-shareable-token   # or
SKYHUB_SERVER_PUBLIC_CAPTURES=true
```

Either one unlocks `/api/captures/current` and `/api/captures/latest` and nothing
else - no archive, no telemetry, no settings, no writes.
