# Notes for the maintainer

## Publishing the first time

1. Create an empty **public** repository named `hearth_calendar` on GitHub under `hartk1213`. Don't add a README, license or .gitignore there; this repo already has them.
2. Push this folder to it (see the main instructions you were given, or use GitHub Desktop).
3. Open the **Actions** tab. The *Test and publish* workflow runs the tests, then builds the Docker image. The first build takes about 5 to 10 minutes because it builds for both PC and Raspberry Pi.
4. **Make the image public** (it starts out private): on your GitHub profile, open **Packages → hearth → Package settings → Change visibility → Public**. Until you do this, Unraid and Docker can't download it.

## Releasing an update

1. Make your changes, then bump `__version__` in `server.py` and add an entry to `CHANGELOG.md`.
2. Commit and push to `main`. That publishes `ghcr.io/hartk1213/hearth:latest`.
3. Optionally tag a version, e.g. `git tag v1.1.0 && git push --tags`. That also publishes `:1.1.0` and `:1.1`.

Unraid users then see **apply update** in their Docker tab.

## Moving your own server to the published image

Your current server builds Hearth from a folder in appdata. To switch to the published image, edit `/mnt/user/appdata/hearth/docker-compose.yml`:

- delete the line `build: /mnt/user/appdata/hearth`
- change `image: hearth:latest` to `image: ghcr.io/hartk1213/hearth:latest`

Then run `docker compose pull && docker compose up -d`. Your data stays in MariaDB, so nothing is lost.
