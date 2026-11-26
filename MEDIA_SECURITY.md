# Media download security

Deployed on blink, 24 November 2026. This guard is deterministic: no LLM calls or
paid scanning. It processes new downloads only, not the existing Plex library or
missing-episode backlog. Plex deletion remains enabled.

## Flow and boundaries

Transmission writes `/quarantine`, backed by `/mnt/Modi/download-quarantine`.
Sonarr/Radarr cannot see that directory. Once complete, the guard pauses the
torrent, moves it to `/mnt/Modi/media-security-stage/<hash>`, validates the exact
file manifest, and inspects content. Successful inspection publishes to the
existing `/mnt/Modi/Kodi/downloads/complete/<category>` location. Transmission is
repointed there and resumes its existing seeding policy.

Transmission mounts only its config, quarantine, and **read-only** approved
files. Read-only matters: approved files can share hardlinks with library files.
Sonarr/Radarr keep their unified `/data` mounts, empty Transmission Directory
fields and no remote path mappings. Plex keeps its writable library mounts.

The controller is confined with Linux Landlock. A private pre-fork broker can
launch only ffprobe, file and ClamAV inside Bubblewrap, with read-only payload
mounts, no network, no credentials and a temporary scratch filesystem. Landlock
cannot itself launch mount namespaces, hence the separate narrow broker.
The service has a 4 GiB memory ceiling, limited swap, one CPU and low I/O priority.

The NTFS-3G mount rejects `renameat2(RENAME_NOREPLACE)`. Directory moves use
ordinary same-filesystem rename after collision checks under a shared worker
lock; a nonempty destination cannot be overwritten. Regular-file moves use
exclusive hardlink creation followed by unlink, with recovery for interruption
between those steps. Scanner manifests record inode/size/mtime. Do not manually
modify intake/staging/approved paths while a job is running.

## Inspection and holds

Allowed videos: MKV, MP4/M4V, AVI, MOV, MPG/MPEG, TS/M2TS, WebM. Known text
subtitles, NFO/TXT and JPEG/PNG sidecars are permitted after content checks.
Executables, scripts, shortcuts, archives, disc images, unknown extensions,
unsafe paths, links, special files, manifest mismatches and missing video hold
the whole torrent. No payload is ever executed.

ClamAV errors, detections, timeouts, incomplete scans and definitions older than
48 hours hold the download. Freshness checks both database date and update time.
Videos above **2047 MiB** receive isolated media validation but cannot receive a
full antivirus scan; the record explicitly says
`media validated; full antivirus scan unavailable`. Eligible sidecars are still
scanned. Oversized non-video files are held. Validation and antivirus reduce
risk; neither proves a file harmless.

Scans defer below 5 GiB available RAM, during Plex transcoding, when Plex cannot
be checked, or below 10 GiB free on Modi. Incoming files stay invisible to Arr.
A stale/dead worker or unreadable guard database also disables destructive
cleanup. All reaper/stall/orphan cleanup holds the same lock as publication.
Transmission cannot delete approved data through its read-only mount; its entry
can still be removed, and the existing orphan sweeper later reclaims approved
files after its normal age buffer.

## Owner commands

Run from `/opt/warden`:

```bash
python3 -m warden.media_guard health
python3 -m warden.media_guard list
python3 -m warden.media_guard show HASH
python3 -m warden.media_guard retry HASH
```

`retry` re-runs checks; it does not bypass them. For a fully staged, complete,
structurally valid payload only, an explicit owner override is available:

```bash
python3 -m warden.media_guard release HASH --accept-risk --reason "Reason reviewed"
```

Overrides are recorded. Unsafe paths and unapproved file types cannot be
released by this command. Holds are never automatically deleted or replaced.
No public HTTP endpoint or new external notifications were added.

Services:

```bash
systemctl --user status media-guard.service
systemctl --user status media-signatures.timer media-update-health.timer
journalctl --user -u media-guard.service --since today
journalctl --user -u media-signatures.service --since today
```

Code: `/opt/warden/warden/media_guard` (source in this repository).
Private config: `/opt/media-server/security/config.json` (never print/share).
SQLite audit and health: `/opt/media-server/security/state`.
Signature updates: daily at approximately 05:15, retrying failures/resource
deferrals hourly; updater visibility: 04:30. Signature verification and payload
scanning share a lock so their ClamAV engines cannot compete for memory.
Watchtower remains the existing container updater. Update visibility reports
failed pulls and fetched images that have not been applied. It does not start
a second updater. Scanner binaries are extracted Ubuntu packages; inspect their
candidate versions during maintenance and refresh the runtime when needed.

Movie profiles exclude CAM, TELESYNC, TELECINE, WORKPRINT, DVD-R and BR-DISK.
Ordinary SD/DVD/WEB/Blu-ray files, original-language profiles, size limits and
selective anime monitoring are preserved. Recyclarr's existing managed HD
profile already excludes these formats; its configuration is unchanged.

## Deployment and rollback

`deploy/install_media_guard.py prepare` installs code/runtime/config/units.
Download `clamav clamav-freshclam clamav-base libclamav12 libmspack0t64` using
Ubuntu apt into `/tmp/media-guard-debs` first. The cutover command is a **one-time
migration**, not a routine update command; it requires all nine authorised films
imported and no Transmission entries. It stops Arr and rechecks intake to close
the preparation race. For subsequent code updates, stop the guard first (intake stays isolated),
run prepare, then start the guard. Run unit tests and sandbox fixtures before deploying scanner changes.

Backups of compose, Transmission settings, Arr/Prowlarr databases/settings,
cleanup scripts and prior container image IDs are under
`/opt/media-server/security/backups/20261124-102914`. Plex preferences/database
were also saved before its update. Backups contain secrets; keep them private.

Safe rollback order:

1. Stop Sonarr/Radarr and pause Transmission intake first.
2. Stop the guard; preserve its database, quarantine and private staging.
3. Restore backed-up compose/settings/cleanup code as needed, with Transmission
   stopped while changing its settings file. Never point Arr at quarantine.
4. Restore service availability only after reviewing every unfinished job.
   Do not move held files into the old completed directory as part of rollback.

The Docker host daemon has a newer candidate package but requires administrator
sudo authentication, unavailable to this session. This is separate from the
updated media containers. Plex player applications on TVs/phones/computers also
require updates on those devices; server updates do not update player apps.

## Verification

```bash
.venv/bin/python -m pytest -q tests/media_guard tests/test_reaper.py
```

Coverage includes crash recovery at both moves, link/unlink recovery, collisions,
metadata rejection, scanner failures and file-count mismatches, stale definitions,
large-video labeling, low-resource deferral and cleanup exclusion/DB failure.
Live checks use generated media only: clean MP4+SRT, EICAR hold, denied library
access, Transmission publication/reverification, and isolated Radarr import.
No existing-library rescans or new requested-film grabs are part of verification.
