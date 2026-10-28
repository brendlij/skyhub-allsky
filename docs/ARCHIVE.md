# Archive and disk protection

A Pi's SD card is small and a camera never stops. SkyHub protects it in three layers,
so a full disk pauses capture instead of crashing the system.

| Layer | Where | Trigger | Effect |
|---|---|---|---|
| Node guard | node | free < `SKYHUB_NODE_MIN_FREE_DISK_GB` (5) | capture pauses, resumes above `..._RESUME_FREE_DISK_GB` (8) |
| Upload guard | server | free < `SKYHUB_SERVER_MIN_FREE_DISK_GB` (3) | `POST /api/captures/upload` answers `507` |
| Archive | server | free < `SKYHUB_SERVER_ARCHIVE_FREE_TRIGGER_GB` (15) | verified local copies are deleted, oldest day first, until free > `..._TARGET_GB` (25) |

The node deletes its own copy of a frame after a successful upload
(`SKYHUB_NODE_KEEP_LOCAL_CAPTURES=true` keeps it). A failed upload no longer ends the
sequence: the frame stays on disk and the next one is tried.

## The archive

Frames are copied to a second location, **read back and hash-checked**, and only then
become deletable. A local file is removed only if

1. its copy was verified (SHA-256 recorded in `archived_files`),
2. the copy is still on the archive with the same size, and
3. it is not from today and no processing session is still working on that day.

If the archive is unreachable nothing is deleted. Space then runs down to the node
guard, which pauses capture. That is the intended outcome: a paused camera, never a
lost night.

`derived/` (timelapses, keograms, startrails) is copied but never deleted locally,
because the database refers to those files. `thumbnails/` are small and stay.

### Enable it

Everything is configured in **Settings -> Storage -> Archive**: pick the destination
folder (mounted shares and drives are listed), press *Test destination*, choose what to
copy, set the free-space thresholds and switch it on. Saving refuses a destination that
is missing, read-only or on the same disk as the data.

The settings live in the database. The `SKYHUB_SERVER_ARCHIVE_*` variables only seed
them the first time the server starts; after that the UI is the source of truth.

`archive_require_mount` ("Require a separate disk") exists because an unmounted share is
just an empty folder on the SD card: copying into it would fill the card and look like a
successful backup.

API, for scripts: `GET/PUT /api/archive/settings`, `POST /api/archive/test`,
`GET /api/archive/mounts`, `GET /api/archive/status`, `POST /api/archive/run`.

### Mounting a NAS (SMB, e.g. UGREEN)

Create the share and a user on the NAS, then on the Pi:

```bash
sudo apt install cifs-utils
sudo mkdir -p /mnt/nas
printf 'username=allsky\npassword=CHANGE-ME\n' | sudo tee /root/.nas-credentials >/dev/null
sudo chmod 600 /root/.nas-credentials
```

`/etc/fstab`:

```
//NAS-IP/allsky  /mnt/nas  cifs  credentials=/root/.nas-credentials,uid=admin,gid=admin,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=15  0  0
```

```bash
sudo mkdir -p /mnt/nas/allsky && sudo systemctl daemon-reload && sudo mount -a
```

`nofail` keeps the Pi booting when the NAS is off. NFS works the same way; any mounted
path is fine.
