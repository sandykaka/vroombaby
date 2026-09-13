# Firmware releases

`current.json` is the only thing that publishes a release. Devices check it every
12 hours and install anything with a higher version than they are running.

Images are hosted on **GitHub Releases**, not here. That keeps megabyte binaries out
of git history — where they would live forever, since git never forgets a blob — and
it means a device whose survival depends on updates does not also depend on
vroombaby.com being reachable.

## Publishing a release

1. Build, and note the version the device will report:

   ```sh
   cd ~/esp/castmute-fw
   idf.py build
   grep CONFIG_APP_PROJECT_VER sdkconfig
   ```

2. Upload the image as a release asset:

   ```sh
   VER=1.0.1
   cp build/castmute_fw.bin /tmp/castmute-$VER.bin
   gh release create fw-$VER /tmp/castmute-$VER.bin \
       --repo sandykaka/vroombaby \
       --title "CastMute firmware $VER" \
       --notes "..."
   ```

3. Record the digest and URL:

   ```sh
   shasum -a 256 /tmp/castmute-$VER.bin
   gh release view fw-$VER --repo sandykaka/vroombaby --json assets \
       --jq '.assets[0].url'
   ```

4. Edit `current.json` with the version, that URL and that digest. The manifest view
   refuses to publish a release whose URL is not HTTPS or whose digest is not 64 hex
   characters, so a typo shows up in your own logs rather than as every unit in the
   field failing to download.

5. Confirm what devices will be told, *before* they are told it:

   ```sh
   curl -s https://www.vroombaby.com/castmute/firmware/manifest.json | python3 -m json.tool
   ```

## Rolling back

Point `current.json` at an earlier image, but **give it a higher version number** —
devices only install something newer than what they are running, so reissuing 1.0.0
to a fleet on 1.0.1 does nothing. Build 1.0.2 from the older source instead.

The bootloader separately rolls back on its own if a new image fails to reach the
network and start discovery. That covers a broken build; this covers a build that
runs but behaves wrongly.

## A caution

Every unit checks in twice a day and there is no staged rollout yet, so whatever is
named here reaches the whole fleet within half a day. Until that exists, treat an
edit to this file as a deployment to every customer at once.

Staged rollout needs firmware support too — the device has to decide whether it is
in the current slice, which means hashing its own MAC against a percentage in the
manifest. Worth building before there are more than a handful of units.
