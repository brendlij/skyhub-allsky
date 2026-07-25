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
