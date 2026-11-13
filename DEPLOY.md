# Deploying Hamsa Homeopathy on a Hostinger VPS

This guide takes you from an empty Hostinger VPS to a running clinic system, step by step.

* **Section A** sets the server up **without a domain**. The site runs at `http://<server-ip>` so you can test it.
* **Section B** adds the domain later. The site then runs at `https://<domain>` with automatic HTTPS certificates.
* **Section C** covers updates and the checklist to finish before real patients are entered.

> ⚠️ **IP mode is for testing only.** Without a domain, the site uses plain HTTP: passwords and patient
> details travel over the internet unencrypted. **Do not enter real patient data until Section B is done**
> and the site opens with `https://`.

How it fits together:

```
Browser ──HTTPS──▶ Caddy (on the server, ports 80/443, certificates)
                     └──▶ nginx container (127.0.0.1:8080 only) ──▶ web container (Django/Gunicorn) ──▶ db container (PostgreSQL)
                                                     uploaded files: "media" volume      database: "pgdata" volume
```

In the commands below, replace anything in `<angle brackets>` with your own value and leave out the brackets.
`<server-ip>` is the VPS's public IPv4 address. `clinicadmin` is the Linux user you'll create; you can pick another name.

---

## Section A: first setup, without a domain

### A1. Create the VPS

1. In Hostinger **hPanel → VPS**, buy or open your VPS plan. KVM 1 (1 vCPU, 4 GB RAM) is enough for one clinic; KVM 2 gives more headroom.
2. **Operating system:** choose plain **Ubuntu 24.04** (*not* a control panel or app template).
3. **Location:** pick the data centre closest to the clinic.
4. Set a long, random **root password** and save it in a password manager.
5. When the VPS is ready, copy its **IPv4 address** from the VPS overview page. That address is `<server-ip>`.

### A2. Make an SSH key on your computer

An SSH key is a pair of files that lets you sign in without a password. On Windows, open **PowerShell**:

```powershell
ssh-keygen -t ed25519 -C "hamsa-vps"
```

Press Enter to accept the default file location, and choose a passphrase you'll remember.
The public half is in `C:\Users\<you>\.ssh\id_ed25519.pub`. It is safe to share. Never share the file *without* `.pub`.

Copy the public key to the server (you'll be asked for the root password once):

```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@<server-ip> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
```

> If you ever lock yourself out, hPanel → VPS → **Browser terminal** still works. Keep it in mind.

### A3. Create your own sudo user

Sign in as root, create a normal user that can use `sudo`, and give it your SSH key:

```bash
ssh root@<server-ip>
adduser clinicadmin                 # choose a strong password (sudo will ask for it)
usermod -aG sudo clinicadmin
rsync --archive --chown=clinicadmin:clinicadmin ~/.ssh /home/clinicadmin
exit
```

Check that the new user works, from a **new** PowerShell window:

```powershell
ssh clinicadmin@<server-ip>
sudo whoami          # must print: root
```

From now on, always sign in as `clinicadmin`.

### A4. Get the code onto the server (GitHub deploy key)

A deploy key gives this one server read-only access to this one repository.

```bash
sudo mkdir -p /opt/hamsa && sudo chown clinicadmin:clinicadmin /opt/hamsa
ssh-keygen -t ed25519 -f ~/.ssh/hamsa_deploy -N "" -C "hamsa-vps-deploy"
cat ~/.ssh/hamsa_deploy.pub
```

On GitHub, open the repository → **Settings → Deploy keys → Add deploy key**. Paste the line printed above, name it `hamsa-vps`, and leave **"Allow write access" unticked**.

Then tell SSH to use that key for GitHub, and clone:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/hamsa_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
git clone git@github.com:<github-user>/<repository>.git /opt/hamsa     # answer "yes" to the fingerprint question
cd /opt/hamsa
```

### A5. Run the server setup script

```bash
cd /opt/hamsa
sudo scripts/setup_server.sh
```

It takes a few minutes. It installs Docker and Caddy, sets up the firewall (only SSH 22, HTTP 80 and HTTPS 443 are open), fail2ban (blocks password-guessing) and automatic security updates. It also points Caddy at the app in IP mode.

Sign out and back in once (`exit`, then `ssh clinicadmin@<server-ip>`), so you can run `docker` without `sudo`.

### A6. Switch off root and password logins for SSH

The setup script deliberately does **not** do this, so a mistake can't lock you out. Do it yourself now:

1. Make sure `ssh clinicadmin@<server-ip>` works **with your key** (A3).
2. Create the settings file:

   ```bash
   sudo nano /etc/ssh/sshd_config.d/01-hardening.conf
   ```

   Put in these three lines, then save (Ctrl+O, Enter) and exit (Ctrl+X):

   ```
   PermitRootLogin no
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   ```

   The name must start with `01-`. SSH uses the *first* value it reads, and Hostinger's `50-cloud-init.conf` may turn passwords back on.
3. Check and apply:

   ```bash
   sudo sshd -t && sudo systemctl restart ssh
   sudo sshd -T | grep -Ei 'permitrootlogin|passwordauthentication'    # expect: no / no
   ```

4. **Keep this window open.** In a new window, check that `ssh clinicadmin@<server-ip>` still works, and that `ssh root@<server-ip>` is refused.

### A7. Fill in the settings (.env)

```bash
cd /opt/hamsa
cp .env.example .env
chmod 600 .env                       # only you can read it: it holds the passwords
openssl rand -base64 48 | tr -d '\n/+=' ; echo     # run 2x: one value for the secret key, one for the DB password
nano .env
```

Fill in at least these:

| Setting | Value |
|---|---|
| `DOMAIN` | **leave empty** for now |
| `SERVER_IP` | `<server-ip>` |
| `DJANGO_SECRET_KEY` | the first random value |
| `POSTGRES_PASSWORD` | the second random value |
| `DJANGO_ADMIN_PASSWORD` | a strong password for the first `admin` user (at least 8 characters, not a common word, not only numbers) |
| `CLINIC_NAME`, `CLINIC_ADDRESS`, `CLINIC_PHONE` | the clinic's details |

Keep `DJANGO_DEBUG=false` and `SEED_DEMO_DATA=false`. Save all these values in your password manager.

> `POSTGRES_PASSWORD` is only applied when the database is created for the very first time. Changing it in
> `.env` later does **not** change the database password, and the app then can't connect.

### A8. First start

```bash
cd /opt/hamsa
scripts/deploy.sh
```

The first build takes a few minutes. It ends with `OK: Deployed …` and a table in which `db`, `web` and `nginx` show **healthy**.
The web log shows the IP-mode warning, as expected:

```bash
docker compose -f docker-compose.prod.yml logs web | grep -E "IP MODE|admin"
```

### A9. The administrator account

On first start the app creates the user `admin` (or your `DJANGO_ADMIN_USERNAME`) with `DJANGO_ADMIN_PASSWORD`. The log line `Created admin 'admin'` confirms it. If the password was too weak, the log says why. Fix it in `.env` and run `scripts/deploy.sh` again.

Once you have signed in (A10), remove the password from `.env`. It is never used again:

```bash
nano .env      # make it:  DJANGO_ADMIN_PASSWORD=
```

### A10. Open the site

Open **`http://<server-ip>`** in a browser and sign in as `admin`. Create the staff accounts (Doctor, Reception, Lab) from the Users screen.

Use made-up test patients only, never real ones (see the warning at the top).

### A11. Daily jobs and nightly backups (cron)

```bash
timedatectl | grep "Time zone"        # Hostinger servers are usually on UTC
sudo crontab -e                       # choose nano if asked
```

Paste the two lines from [deploy/crontab.example](deploy/crontab.example) that match the server's time zone. If the clock is UTC, use `30 2 * * *` for the 08:00 IST daily jobs and `30 20 * * *` for the 02:00 IST backup (India is UTC + 5:30). Save and exit.

To see what ran: `journalctl -t hamsa-daily` and `journalctl -t hamsa-backup`.

### A12. Backups: passphrase, off-site copy, first backup

Backups are encrypted with a passphrase that only root can read.

```bash
sudo sh -c 'openssl rand -base64 32 > /root/.hamsa-backup-passphrase && chmod 600 /root/.hamsa-backup-passphrase'
sudo cat /root/.hamsa-backup-passphrase
```

**Copy that passphrase into your password manager now.** Without it, no backup can ever be opened, and if the server is lost, the copy on the server is lost with it.

**Off-site copy with rclone.** A backup that lives only on the server is lost along with the server. Pick any storage rclone supports, for example a Backblaze B2 bucket (cheap) or Google Drive:

```bash
sudo rclone config       # n = new remote, give it a name such as "b2", follow the prompts
sudo rclone lsd b2:      # test: lists your buckets
```

In `.env`, set `BACKUP_RCLONE_REMOTE=b2:<bucket-name>/hamsa` (remote name, a colon, then the folder).

Run the first backup by hand:

```bash
cd /opt/hamsa
sudo scripts/backup.sh
sudo ls -lh /root/backups
sudo rclone ls b2:<bucket-name>/hamsa
```

Each run makes two encrypted files: `hamsa-db-<date>.dump.gpg` (database) and `hamsa-media-<date>.tar.gz.gpg` (uploaded files). They belong together. Local copies older than 30 days are deleted automatically. Set a retention rule in the bucket as well (e.g. keep 90 days).

### A13. Test a restore

An untested backup is not a backup. While the server holds only test data, practise the real thing:

1. In the app, create a test patient called **"Restore Test"** and attach a small file to it.
2. Run `sudo scripts/backup.sh`.
3. Delete the "Restore Test" patient in the app.
4. Restore the backup you just made (use the file names from `sudo ls /root/backups`):

   ```bash
   sudo scripts/restore.sh /root/backups/hamsa-db-<date>.dump.gpg /root/backups/hamsa-media-<date>.tar.gz.gpg --wipe
   ```

   The script asks for the database name and then the word `RESTORE` before it changes anything. `--wipe` is needed because the database is not empty. On a brand-new server you would leave it out.
5. Sign in: "Restore Test" and its attachment are back.

To restore from the off-site copy, first fetch the files: `sudo rclone copy b2:<bucket-name>/hamsa /root/restore --include "hamsa-*<date>*"`.

### A14. Uptime monitoring (UptimeRobot)

1. Create a free account at [uptimerobot.com](https://uptimerobot.com).
2. **Add New Monitor** → type **HTTP(s)**, URL `http://<server-ip>/readyz`, interval 5 minutes.
3. Add your e-mail (and phone) as alert contacts.

`/readyz` answers 200 only when the app *and* the database work, so you'll hear about either failing.

---

## Section B: adding the domain (HTTPS)

### B1. Point the domain at the server (Hostinger DNS)

hPanel → **Domains** → your domain → **DNS / Nameservers** → **DNS records**:

| Type | Name | Points to | TTL |
|---|---|---|---|
| A | `@` | `<server-ip>` | 300 |
| A | `www` | `<server-ip>` | 300 |

* **Delete** any other `A` or `CNAME` records for `@` and `www` (for example Hostinger's parking page). A `CNAME` for `www` pointing to your domain also works.
* **Delete any `AAAA` records** for `@` and `www`, unless they point to this VPS's IPv6 address. Otherwise certificate checks can go to the wrong place.

### B2. Wait for DNS

This usually takes minutes, but can take a few hours. Check from the server:

```bash
dig +short <domain> @1.1.1.1
dig +short www.<domain> @1.1.1.1
```

Both must print `<server-ip>`. You can also check from around the world at [dnschecker.org](https://dnschecker.org).

### B3. Switch the site to the domain

```bash
cd /opt/hamsa
sudo scripts/set_domain.sh <domain>          # e.g. sudo scripts/set_domain.sh example.com
```

The script:
1. checks the DNS again, and stops without changing anything if it's not right yet;
2. writes `DOMAIN=<domain>` to `.env`;
3. installs the HTTPS Caddy config (Caddy fetches Let's Encrypt certificates for `<domain>` and `www.<domain>` and renews them automatically);
4. restarts the app;
5. checks that `https://<domain>/readyz` answers.

From now on, secure cookies, the HTTP → HTTPS redirect and HSTS are on automatically. `http://<server-ip>` no longer serves the app. To move to a different domain later, run the same command with the new domain.

### B4. Run Django's deployment check

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py check --deploy
```

The only expected line is **`security.W021`** (HSTS preload is off). That is deliberate: preload is very hard to undo, and the clinic doesn't need it. Anything else needs fixing before go-live.

Then open `https://<domain>` and `http://<domain>` (it should jump to https), and sign in.

### B5. Point UptimeRobot at the domain

Edit the monitor's URL to `https://<domain>/readyz`.

### B6. After a week without problems: long HSTS

HSTS tells browsers "only ever use HTTPS for this site". It starts at one hour (`HSTS_SECONDS=3600`), so a problem in the first days is easy to undo. After a week with no problems:

```bash
cd /opt/hamsa
nano .env                         # HSTS_SECONDS=31536000   (one year)
docker compose -f docker-compose.prod.yml up -d --force-recreate web
```

---

## Section C: running the clinic

### C1. Updating to a new version

```bash
cd /opt/hamsa
sudo scripts/backup.sh            # always back up first
scripts/deploy.sh
```

`deploy.sh` pulls the latest code, rebuilds, restarts (database migrations run automatically), waits until the app answers `/readyz` and prints the container status. A failed update leaves the error on screen; see the logs below.

Useful commands (run in `/opt/hamsa`):

| What | Command |
|---|---|
| Status | `docker compose -f docker-compose.prod.yml ps` |
| App logs (live) | `docker compose -f docker-compose.prod.yml logs -f --tail 100 web` |
| Restart the app | `docker compose -f docker-compose.prod.yml restart web` |
| Caddy / HTTPS logs | `sudo journalctl -u caddy --since "1 hour ago"` |
| Django shell commands | `docker compose -f docker-compose.prod.yml exec web python manage.py <command>` |

### C2. Before going live (real patient data)

- [ ] **HTTPS:** Section B is done, `https://<domain>` works, and `check --deploy` shows only W021.
- [ ] **SSH:** root login and password login are off (A6), and key login works.
- [ ] **All passwords changed**, none reused from testing:
  - the `admin` app password (change it in the app) and every staff password;
  - `DJANGO_ADMIN_PASSWORD` is empty in `.env`;
  - the Hostinger root password; hPanel, GitHub and your e-mail have two-factor login switched on.
- [ ] **Demo data off:** `SEED_DEMO_DATA=false` in `.env`. If demo data was ever loaded, delete or deactivate the demo users (`doctor`, `reception`, `lab`), because their password is published in the README.
- [ ] **Test data removed.** Delete test patients in the app, or start completely fresh (this **erases the database and uploads**):

  ```bash
  docker compose -f docker-compose.prod.yml down
  docker volume rm hamsa_pgdata hamsa_media
  nano .env            # set DJANGO_ADMIN_PASSWORD again for the new admin
  scripts/deploy.sh
  ```

- [ ] **Backups tested:** the cron job ran (`journalctl -t hamsa-backup`), the files are in the off-site storage, a restore worked (A13), and the passphrase is in your password manager.
- [ ] **Monitoring:** UptimeRobot watches `https://<domain>/readyz` and sends you alerts.
- [ ] `DJANGO_DEBUG=false` in `.env` (the app refuses to start in domain mode otherwise).
