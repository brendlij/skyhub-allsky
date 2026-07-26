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

## API

Everything the web UI does is available over HTTP, so the server can drive Home
Assistant, Node-RED or your own scripts. See [docs/API.md](docs/API.md) for the
full reference, or browse the live schema at `http://<server>:8000/docs`.

Authentication is off by default. To require an API key on every `/api` route and
both WebSockets, set one on the server and give the same value to each node:

```bash
SKYHUB_SERVER_API_KEY=pick-something-long-and-random python skyhub.py server
SKYHUB_NODE_API_KEY=pick-something-long-and-random python skyhub.py node
```

The web UI asks for the key the first time it gets a 401 and remembers it in that
browser. Without a key set, the server stays open to anyone who can reach the
port - fine on a trusted LAN, not for anything reachable from the internet.

To publish the latest image somewhere without handing out that key - which can also
change settings and stop capture - open up the current image on its own:

```bash
SKYHUB_SERVER_PUBLIC_CAPTURE_TOKEN=some-shareable-token   # or
SKYHUB_SERVER_PUBLIC_CAPTURES=true
```

Either one unlocks `/api/captures/current` and `/api/captures/latest` and nothing
else - no archive, no telemetry, no settings, no writes.
