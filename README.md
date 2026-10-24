# Hamsa Homeopathy – Case Record Management System

Django 5 · Django REST Framework · PostgreSQL · Bootstrap 5 · Docker · Kubernetes

A role-based patient case-record system for a homeopathic clinic. The case-record
screen follows the paper *Homeopathic Case Record* form section by section: patient
details, case history, physical and mental generals, examination, diagnosis,
investigations, past treatment, before/after images, repertory analysis,
prescription and follow-up history.

---

## Quick start (local, no Docker)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on Linux/macOS)
pip install -r requirements.txt

# Quickest: SQLite for a local trial
set DB_ENGINE=sqlite & set DJANGO_DEBUG=true          # PowerShell: $env:DB_ENGINE="sqlite"; $env:DJANGO_DEBUG="true"
python manage.py migrate
python manage.py seed_demo         # demo users + 12 sample patients
python manage.py runserver
```

Open http://127.0.0.1:5000 and sign in with one of the demo accounts. All of them use the password `Hamsa@2026`; change it with `seed_demo --password …`.

| Username    | Role                    |
|-------------|-------------------------|
| `admin`     | Administrator (also Django superuser) |
| `doctor`    | Doctor                  |
| `reception` | Attender / Receptionist |
| `lab`       | Lab Assistant           |

To use PostgreSQL locally, leave `DB_ENGINE` unset and set the `POSTGRES_*` variables from `.env.example`.

Run the tests with `DB_ENGINE=sqlite python manage.py test`.

### Use it from other devices on the same Wi-Fi

```powershell
$env:DB_ENGINE="sqlite"; $env:DJANGO_DEBUG="true"; $env:DJANGO_ALLOW_LAN="true"
python manage.py runserver 0.0.0.0:5000
```

`DJANGO_ALLOW_LAN=true` adds this PC's current IP addresses and hostname to `ALLOWED_HOSTS`. Other phones, tablets and PCs on the Wi-Fi then open `http://<this-PC's-IP>:5000`. Find the IP with `ipconfig` (the "Wi-Fi → IPv4 Address" line).

* Windows Firewall must allow Python inbound on **Private** networks, and the Wi-Fi must be marked Private.
* Some routers enable "AP / client isolation", which blocks devices on the same network from reaching each other.
* `DJANGO_DEBUG=true` shows error details to anyone on the network. For day-to-day clinic use, run with debug off and a real `DJANGO_SECRET_KEY`.

---

## Deploy on Vercel (online)

Vercel runs the app as a serverless function. It detects `manage.py`, runs `collectstatic` and serves static files from its CDN. [vercel.json](vercel.json) sets the region to Mumbai (`bom1`) and a daily cron job. Vercel has no database or file disk, so you need two free add-ons:

| Need | Use | Settings to add in Vercel → Project → Settings → Environment Variables |
|---|---|---|
| Database | **Neon PostgreSQL** (Vercel → Storage → *Create* → Neon; choose region **AWS ap-south-1 Mumbai**) | `DATABASE_URL` is added automatically by the integration |
| Uploaded files | **Cloudflare R2** bucket (private) + an API token with read/write on it | `S3_BUCKET_NAME`, `S3_ENDPOINT_URL` (`https://<account-id>.r2.cloudflarestorage.com`), `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` |
| Uploaded files (no credit card) | **Backblaze B2** private bucket (10 GB free) + an application key with read/write on it | `S3_BUCKET_NAME`, `S3_ENDPOINT_URL` (`https://s3.<region>.backblazeb2.com`), `S3_REGION` (e.g. `eu-central-003`), `S3_ACCESS_KEY_ID` (keyID), `S3_SECRET_ACCESS_KEY` (applicationKey) |
| App secrets | — | `DJANGO_SECRET_KEY` (long random text), `CRON_SECRET` (random text), optionally `CLINIC_NAME`, `TIME_ZONE=Asia/Kolkata` |

Steps:

1. In [vercel.com](https://vercel.com), choose **Add New → Project → Import** the `hamsahomeopathy` GitHub repo. Framework: *Django* (detected).
2. Add the Neon database and the environment variables above, then **Deploy**.
3. **Create the tables:** on this PC, run `powershell -ExecutionPolicy Bypass -File scriptsercel_migrate.ps1`. Paste the Neon `DATABASE_URL` when asked and choose the online `admin` password. Add `-SeedDemo` to load demo data. Run it again whenever an update adds migrations.
4. Open `https://<project>.vercel.app` and sign in as `admin`.

Limits of the Vercel version:

* **4.5 MB per upload/save:** Vercel rejects larger requests, and the app warns before sending. Compress big PDFs or scans, or upload them one at a time.
* **File storage:** uploaded files live in the private R2 bucket. The app checks the user's role, then redirects to a signed link that is valid for 5 minutes.
* **Daily cron:** missed follow-ups and reminders run once a day at 08:00 IST through Vercel Cron (`/cron/daily/`, protected by `CRON_SECRET`).
* **Notifications:** the bell polls every 12 seconds, so each open browser tab makes regular function calls.
* **Data location:** patient data is stored with Neon and Cloudflare, not on the clinic PC.
* **Package name:** the Django REST app lives in `restapi/` rather than `api/`, because Vercel reserves the top-level `api/` folder. Web addresses (`/api/...`) are unchanged.

## Deploy on Render (online)

The repo includes a [render.yaml](render.yaml) Blueprint: a Docker web service (Singapore region) with a 1 GB persistent disk for uploads, and a PostgreSQL database.

1. Push this repo to GitHub (done).
2. In [Render](https://dashboard.render.com), go to **New + → Blueprint** and pick the `hamsahomeopathy` repo.
3. Enter a strong value for **`DJANGO_ADMIN_PASSWORD`** when asked. The first login will be user `admin` with that password. Render generates `DJANGO_SECRET_KEY` for you.
4. Click **Apply**. The first build takes a few minutes. Migrations run automatically on every deploy, and the site appears at `https://hamsa-homeo.onrender.com` (or the name Render shows).

Notes:

* **Cost:** the web service uses a paid instance (`0.5c-512mb`), because Render only offers persistent disks on paid instances. Without a disk, uploaded photos and reports would be lost on every deploy.
* **Database plan:** the database starts on Render's `free` plan, which is time-limited. Change `plan:` to `0.1c-256mb` (or larger) before storing real patient data, and turn on backups.
* **Demo data:** to load it once, set `SEED_DEMO_DATA=true` in the service's Environment tab, deploy, then set it back to `false`. Change the demo passwords afterwards.
* **Scheduled jobs:** follow-up reminders and marking missed follow-ups are not scheduled on Render by default. Add a Render Cron Job running `python manage.py mark_missed_followups && python manage.py send_followup_reminders` with the same environment, or run it from the Render shell.
* **Settings:** the app reads Render's `DATABASE_URL` and `RENDER_EXTERNAL_HOSTNAME` automatically. HTTPS redirects, secure cookies and HSTS are switched on through the blueprint's environment variables.

## Docker Compose

```bash
cp .env.example .env               # set DJANGO_SECRET_KEY; SEED_DEMO_DATA=true on first run if you want demo data
docker compose up --build
```

The app runs at http://localhost:5000. Migrations run automatically on start. Uploaded files are kept in the `media` volume and the database in `pgdata`.

---

## Kubernetes (local cluster)

The manifests in [k8s/](k8s/) work on Docker Desktop Kubernetes, minikube, kind or k3d.

| File | Purpose |
|------|---------|
| `00-namespace.yaml` | `hamsa` namespace |
| `01-configmap.yaml` | Non-secret settings (hosts, DB host, clinic name …) |
| `02-secret.yaml` | Django secret key and DB credentials (**development values – replace**) |
| `10-postgres.yaml` | PostgreSQL 16 **StatefulSet** + headless Service + 5 Gi volume claim |
| `20-media-pvc.yaml` | 10 Gi PersistentVolumeClaim for uploaded documents/images |
| `30-web-deployment.yaml` | Django/Gunicorn Deployment; init container runs migrations; liveness `/healthz`, readiness `/readyz` |
| `31-web-service.yaml` | NodePort Service on **30080** |
| `40-followup-reminders-cronjob.yaml` | Daily job: mark missed follow-ups, then send reminders (SMS/WhatsApp, disabled by default) |

### 1. Build the image where the cluster can see it

```bash
docker build -t hamsa-homeo:latest .

# minikube:  minikube image load hamsa-homeo:latest
# kind:      kind load docker-image hamsa-homeo:latest
# k3d:       k3d image import hamsa-homeo:latest
# Docker Desktop Kubernetes uses the local image directly.
```

### 2. Create the secret (recommended) and apply the manifests

```bash
kubectl apply -f k8s/00-namespace.yaml
kubectl -n hamsa create secret generic hamsa-secrets \
  --from-literal=DJANGO_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(50))')" \
  --from-literal=POSTGRES_USER=hamsa \
  --from-literal=POSTGRES_PASSWORD='choose-a-strong-password'

kubectl apply -k k8s/          # if you created the secret above, first remove 02-secret.yaml from kustomization.yaml
kubectl -n hamsa rollout status statefulset/hamsa-postgres
kubectl -n hamsa rollout status deployment/hamsa-web
```

### 3. Load demo data or create the first administrator

```bash
kubectl -n hamsa exec deploy/hamsa-web -- python manage.py seed_demo --password 'Hamsa@2026'
# or a real admin:
kubectl -n hamsa exec -it deploy/hamsa-web -- python manage.py createsuperuser
```

### 4. Open the app

* NodePort: http://localhost:30080 (Docker Desktop). On minikube, run `minikube service -n hamsa hamsa-web --url`.
* Port-forward: `kubectl -n hamsa port-forward svc/hamsa-web 5000:5000`, then open http://localhost:5000.

If you use another hostname or port, add it to `DJANGO_ALLOWED_HOSTS` and
`DJANGO_CSRF_TRUSTED_ORIGINS` in the ConfigMap, then run `kubectl -n hamsa rollout restart deploy/hamsa-web`.

**Production notes**

* Terminate TLS in front of the app, then set:
  * `DJANGO_SECURE_COOKIES=true`
  * `DJANGO_BEHIND_TLS_PROXY=true`
  * `DJANGO_SSL_REDIRECT=true`
  * `DJANGO_HSTS_SECONDS=31536000`
* The media volume is `ReadWriteOnce`, so the web Deployment runs one replica. To run more replicas, switch the PVC to a `ReadWriteMany` storage class.
* Back up both the Postgres volume and the media volume. For the database, use `pg_dump` from the `hamsa-postgres-0` pod.

---

## Roles and access

Every rule lives in one table, [accounts/rbac.py](accounts/rbac.py) (`SECTION_RULES`). HTML views use `section_required` / `can_view` / `can_edit`, templates use the `nav` flags, and the API uses `SectionPermission` / `section_permission`. Nothing checks role names directly.

| Section / screen | Admin | Doctor | Reception (Attender) | Lab |
|---|---|---|---|---|
| Patient identity (OPD No., name, age, sex) | view | view | view | view |
| Patient contact details, address, photo | edit | edit | edit | hidden |
| Check-in (New case / Follow-up) | ✔ | ✔ | ✔ | – |
| Change visit type after check-in | ✔ | ✔ | – | – |
| Full case sheet screen | ✔ | ✔ | ✔ | redirected to lab view |
| Previous-visit grid and modal | view | view | view (no diagnosis/prescription columns) | hidden |
| **Patient history: summary bar, timeline, previous prescriptions, repeat prescription** | ✔ | ✔ | hidden | hidden |
| Case history, Physical generals | edit | edit | view | hidden |
| **Mental generals** | edit | edit | hidden | hidden |
| Follow-up assessment (changes since last visit) | edit | edit | hidden | hidden |
| Physical examination | edit | edit | edit | hidden |
| Diagnosis | edit | edit | hidden | hidden |
| Investigations requested | edit | edit | edit | view |
| Investigation reports | all | all | all, upload | **upload; sees lab-produced reports only** |
| Past treatment | edit | edit | edit | hidden |
| Before/after images | edit | edit | view | hidden |
| Repertory analysis | edit | edit | hidden | hidden |
| **Prescription (full section)** | edit | edit | hidden | hidden |
| Follow-up progress notes | edit | edit | hidden | hidden |
| Schedule / reschedule / cancel follow-ups | ✔ | ✔ | ✔ (date, time, status only) | hidden |
| Follow-up clinical notes | ✔ | ✔ | hidden | hidden |
| **Family links** (manual and auto-linked): see name, OPD No., relationship, "via"; add, edit, confirm, remove | ✔ | ✔ | ✔ | hidden |
| Undo a rejected auto-link | ✔ | – | – | – |
| **History pre-fill** from the patient's own earlier visits | ✔ | ✔ | only in sections they can edit | – |
| **Past history summary, family history panel and auto-fill** | ✔ | ✔ | hidden | hidden |
| **Consult & prescribe for a family member** (through family) | ✔ | ✔ | – | – |
| **Send to pharmacy** | ✔ | ✔ | – | – |
| **Pharmacy queue and order items** (incl. "collect by" orders) | ✔ | status only (case record, today's patients) | ✔ items only | hidden |
| Update dispense status, print labels | ✔ | – | ✔ | – |
| Notification bell | ✔ | ✔ | ✔ | hidden |
| **Lab dashboard** (the lab's home page) | ✔ | – | – | ✔ |
| **Previous reports, per-patient report history** | ✔ | ✔ | – | ✔ (investigation data only) |
| Upload a corrected report | ✔ | ✔ | – | ✔ |
| Superseded report versions | ✔ | ✔ | – | – |
| Reports, CSV export | ✔ | ✔ | – | – |
| Users, audit log | ✔ | – | – | – |
| Delete patients / visits / reports | ✔ | – | – | – |

* **Hidden sections are never rendered or serialised**, and the server ignores posted data for any section the user cannot edit.
* **Default expansion:**
  * Doctor and Admin: Patient, Case history, Mental generals, Diagnosis, Prescription, *Previous prescriptions*, the follow-up comparison and the family-history panel are open.
  * Reception: Patient, Examination and Investigations are open.
* **Uploaded files** are served through an authenticated view that applies the same rules, down to individual reports for the lab.

## Main screens

| Screen | URL | Who |
|---|---|---|
| Home: launcher tiles; today's patients (waiting first, with New/Follow-up, consultation status and pharmacy status); upcoming follow-ups (overdue, today, next 7 days); pharmacy widget; pending lab reports; recent prescriptions | `/` | Admin, Doctor, Reception |
| **Check-in**: choose New case or Follow-up | `/checkin/` | Admin, Doctor, Reception |
| Register a new patient (*Save patient only* or *Save & fill case sheet*, plus linking family members) | `/patients/new/` | Admin, Doctor, Reception |
| Find patient; new case for an existing patient; follow-up check-in | `/patients/`, `?mode=newcase`, `?mode=followup` | Admin, Doctor, Reception |
| Patient page: check-in menu, next follow-up card, **family members** | `/patients/<id>/` | Admin, Doctor, Reception |
| **Case record**: summary bar, previous visits and timeline tabs, follow-up comparison, family-history panel, previous prescriptions with *Repeat*, prescription with *Send to pharmacy* and order status, follow-up scheduling | `/visits/<id>/` | Admin, Doctor (Reception sees its own sections) |
| Full-page patient history (timeline, date filter) | `/patients/<id>/history/` | Admin, Doctor |
| Follow-ups list | `/followups/` | Admin, Doctor, Reception |
| **Pharmacy queue** (New → Preparing → Ready → Dispensed today; designed for tablets) | `/pharmacy/` | Admin, Reception |
| Dispensing slip (A4) / labels (62×40 mm) | `/pharmacy/orders/<id>/label/` (`?size=label`) | Admin, Reception |
| **Lab dashboard**: counts, pending reports with in-place upload, recently completed. This is the lab's `/` | `/lab/` (also `/` for lab users) | Admin, Lab |
| Lab visit view (a single visit's investigations) | `/lab/visits/<id>/` | Admin, Lab |
| **Previous reports** (filters: date, test, uploaded by, patient) and **per-patient report history**; corrected uploads | `/lab/reports/`, `/lab/patients/<id>/reports/` | Admin, Doctor, Lab |
| Printable prescription | `/visits/<id>/prescription/` | Admin, Doctor |
| Reports, users, audit log | `/reports/`, `/accounts/users/`, `/audit/` | as in the table above |
| Swagger UI / ReDoc / OpenAPI schema | `/api/docs/`, `/api/redoc/`, `/api/schema/` | signed-in users |

## Workflows

### Check-in (reception)

1. **New case**
   * **New patient:** register them. *Save patient only* creates today's `NEW` / `WAITING` visit with no case-sheet rows. *Save & fill case sheet* opens the case sheet.
   * **Existing patient, new complaint:** search, then *Start new case*. This gives an empty case sheet; the doctor still sees the full history.
2. **Follow-up:** search for the patient (any scheduled follow-up is shown), then *Check in*. The new visit:
   * is linked to the previous visit;
   * carries forward the case history and generals;
   * adds a follow-up assessment section;
   * marks the scheduled follow-up `COMPLETED`.

Opening a `WAITING` visit as a doctor moves it to `IN_CONSULTATION`. *Save & complete consultation* or *Send to pharmacy* sets it to `COMPLETED`.

### Follow-up scheduling

* **Doctors and reception** can schedule from the case record or the patient page, with quick picks of 1 week, 2 weeks, 1 month and 3 months. They can reschedule, cancel or check in from the dashboard and the follow-ups list.
* **One active follow-up per patient.** This is enforced by a database constraint, and replacing an existing one asks for confirmation.
* **Daily jobs:** `mark_missed_followups` marks past-date follow-ups as `MISSED`. `send_followup_reminders` handles SMS/WhatsApp, which is disabled by default. Both run from the Kubernetes CronJob.

### Family links and family history

* **Linking relatives:**
  * At registration, *"Are any family members already registered with us?"* → search by mobile, name or OPD No. The mobile number just typed is looked up automatically, and matches are offered to link.
  * Links can also be added, edited or removed later from the patient page.
* **Two-way links:** each link is stored in both directions. "Lakshmi is Ravi's Mother" automatically adds "Ravi is Lakshmi's Son", using Ravi's sex. If sex is unknown the reverse is neutral (Child, Sibling, Parent, Grandchild, "Nephew / Niece").
  * Editing or removing one side changes both.
  * Duplicate, self and conflicting links are rejected, partly by database constraints.
* **Shared mobile numbers:** patients may share one; it is indexed but not unique.
* **The doctor's family-history panel** lists each relative with:
  * their diagnoses and dates (duplicates merged, newest first);
  * chronic notes (their past history and constitution);
  * their last visit, and a link to open their record in a new tab with a *Back to …* link.

  Since Phase 3 the family-history field is auto-filled from this panel when it's empty, or new facts are offered with *Add to family history* (see below); the old *Insert* button is gone. Relatives' prescriptions and visits never appear in the patient's own history or timeline.

### Automatic family connections (Phase 3)

When relatives are linked, the rest of the close family is worked out automatically. The rules live in `INFERENCE_RULES` in [clinic/relationships.py](clinic/relationships.py):

| If B is A's… | and C is B's… | then C is A's… |
|---|---|---|
| parent | spouse | parent (Father/Mother by C's sex) |
| parent | child (C ≠ A) | sibling |
| parent | parent | grandparent |
| sibling | parent | parent |
| sibling | sibling (C ≠ A) | sibling |
| child | child | grandchild |
| spouse | child | child |

How inference behaves:

* **Scope:** only close family is inferred (parents, siblings, grandparents, children, grandchildren). Cousins, in-laws, uncles and aunts can still be linked manually.
* **Two-way and sex-aware:** every inferred link is stored both ways, labelled by sex, with neutral labels (Parent, Sibling, Child) when sex is unknown.
* **When it runs:** `rebuild_inferred_links()` runs after every add, edit, confirm, reject or remove. It recomputes the whole connected family in one transaction and is idempotent. Inferred links are chained, so "my mother's husband" becomes "my father", and then "my father's mother" becomes "my grandmother".
* **What is stored:** each link records `source` (`MANUAL` or `INFERRED`), `inferred_via` (shown as "Auto-linked via Mother") and `status` (`ACTIVE` or `REJECTED`).
* **Precedence:** manual links always win.
  * Removing a manual link also removes inferred links that depended only on it.
  * Removing an inferred link marks the pair `REJECTED`, so it is never inferred again until an admin clicks *Undo*.
  * If the rules disagree about a pair, no link is created and an `INFERENCE_CONFLICT` entry is written to the audit log.
* **On screen:**
  * The patient page shows **Linked** or **Auto-linked via …**, with *Confirm* (turns it manual) and *Remove*.
  * After registration, reception sees "Also connected automatically: Ramesh (Father), …".
* **Repairs:** `python manage.py rebuild_family_links` rebuilds every family. Run it once after migrating, or any time.

### History that fills itself (Phase 3)

`HISTORY_FIELDS` in [clinic/prefill.py](clinic/prefill.py) decides what is **stable history** and what is **visit-specific**:

| Section | Stable (pre-filled) | Visit-specific (never pre-filled) |
|---|---|---|
| Case history | past history, family history | presenting complaints |
| Physical generals | all 14: sleep, dreams, thirst, appetite, desires, aversions, urine, stool, sweat, thermals, menses, obstetric history, constitution, habits | – |
| Mental generals | all 16 | – |
| Past treatment | previous medication, duration, outcome, notes | – |
| Follow-up assessment, examination, diagnosis, investigations, prescription | – | everything |

* **Pre-fill:** when a visit that isn't completed opens, each empty stable field is pre-filled from the **most recent earlier visit that recorded it**, looking across all visits.
  * This applies to both new cases and follow-ups; follow-ups also keep the Phase 1 carry-forward.
  * Pre-filled fields have a light background and a "From visit on 12 Mar 2026" tag, which disappears once the field is edited.
  * Each history section has *Refresh from history* and *Clear* buttons.
* **Saving:** values are written only when the form is saved. Opening a page creates nothing. Each history section stores `prefill_sources` (field → source visit) and writes a `HISTORY_PREFILLED` audit entry. Completed visits are never pre-filled.
* **Past history summary:** a read-only panel (Doctor/Admin) at the top of the case history, rebuilt live from records each time. It lists every earlier diagnosis (merged, with dates), remedies with potency and dates, and investigations with report links.
* **Family history:** built from **every active relative, manual and inferred**, using all of their visits.
  * Each relative contributes diagnoses with dates (merged, newest first) and chronic notes from past history and constitution.
  * Relatives are grouped as Parents, Siblings, Grandparents, Children and Spouse, e.g. `Mother (Lakshmi, 58) – Hypertension (2025), Diabetes (2024)`.
  * It works in both directions: linking a parent and child gives each the other's conditions.
  * If the field is **empty**, it is auto-filled, marked "From linked family records".
  * If the field **already has text**, it is never overwritten. Instead the page shows *"New information from linked relatives: …"* with **Add to family history** and **Ignore**.
  * Nothing is ever written to a relative's record.

### Consult & prescribe for a family member (Phase 3)

1. **Start:** in the family panel, the doctor clicks **Consult & prescribe for Lakshmi**, which opens in a new tab with *Back to Ravi*. This creates a visit **for the relative** with:
   * `consultation_mode = THROUGH_FAMILY`;
   * `reported_by` set to the current patient ("Reported by son, Ravi");
   * `FOLLOW_UP` if the relative has earlier visits, otherwise `NEW`.
2. **Consult:** the relative's own history, previous prescriptions, pre-fill and family history all appear. Examination is optional and marked "Patient not present".
3. **Send to pharmacy:** the order belongs to the relative (their name, OPD No. and label) and shows **"Collect by: Ravi (son)"**. In the queue it sits right after Ravi's own order with a *Same pickup as Ravi* badge.
4. **Where it appears:** only in the relative's history and timeline, marked *Consulted through family member*, never in Ravi's record. It is audited (`VISIT_THROUGH_FAMILY`, `COLLECT_BY_ORDER`).

### Lab dashboard and report history (Phase 3)

* **Dashboard** (the lab user's `/`):
  * Pending, Overdue (pending longer than `LAB_OVERDUE_DAYS`, default 1 day) and Completed-today counts.
  * The pending table, oldest first, with overdue rows highlighted, search by OPD No., name or test, and a test-type filter.
  * **Upload report** opens a modal (file + remarks); the row disappears and the counts update without reloading. The report is tagged `LAB`, the investigation is marked completed with `completed_at`, and the requesting doctor gets a bell notification ("Report ready: …").
  * The last 10 uploads.
* **Previous reports:** `/lab/reports/` with filters, plus per-patient report history to compare results over time. These show investigation data and patient identity only.
* **Corrected reports:** a corrected upload creates version *n+1* and keeps the old file as `SUPERSEDED`, visible to Doctor/Admin only. The doctor is notified. Lab users cannot delete reports; only admins can.

### Prescription → pharmacy (reception dispenses)

1. The doctor clicks *Send to pharmacy* (or *Send but keep consultation open*). This saves the case record and freezes the items into a versioned **dispense order**. The order is a snapshot of remedy, potency, form, quantity, dose / repetition, duration and instructions.
2. Reception gets a notification: a bell count, a toast with an optional sound, and a browser notification if allowed. The browser polls `GET /api/notifications/unread/` every 12 seconds and pauses while the tab is hidden.
3. Reception moves the order **New → Preparing → Ready → Dispensed** and prints a slip or labels. The doctor sees the status on the case record and in *Today's patients*.
4. If the doctor changes a prescription after sending it, saving sends a new version automatically:
   * Reception gets an **Updated prescription** alert, with added and changed items highlighted and removed items struck through.
   * The previous open order becomes `SUPERSEDED`. A dispensed order stays `DISPENSED`.

Notifications are delivered through [core/inbox.py](core/inbox.py). To move to WebSockets, set `NOTIFICATION_PUSH_BACKEND` to a channel-layer backend; the callers stay the same.

## REST API

* **Authentication:**
  * `POST /api/auth/login/` returns a token; send it as `Authorization: Token <key>`.
  * `GET /api/auth/me/` returns your role, your home screen, and access for every section.
* **Phase 3:**
  * `GET /api/patients/<id>/relationships/` now includes `source`, `status`, `inferred_via` and `via_label`. New actions: `POST .../relationships/<link_id>/confirm/`, `.../reject/`, and `.../unreject/` (admin).
  * `GET /api/patients/<id>/history-prefill/` and `GET /api/patients/<id>/family-history/` (Doctor/Admin).
  * `POST /api/patients/<relative_id>/visits/` with `{"consultation_mode": "THROUGH_FAMILY", "reported_by": <patient_id>}`. Returns 400 unless the two are linked as family.
  * `GET /api/lab/dashboard/`.
  * `GET /api/investigation-reports/?status=&test=&date_from=&date_to=&uploaded_by=me&patient=`.
  * `POST /api/investigation-reports/<id>/correct/`.
* **Patients:**
  * `patients` (identity-only for the lab)
  * `GET /api/patients/<id>/history/` (Doctor/Admin)
  * `GET|POST /api/patients/<id>/relationships/` and `DELETE /api/patients/<id>/relationships/<link_id>/`
  * `GET /api/patients/<id>/family-history/` (Doctor/Admin)
* **Visits:**
  * `visits`, filterable by `status` and `visit_type`
  * `POST /api/visits/check-in/`
  * `POST /api/visits/<id>/repeat-prescription/`
* **Clinical sections:**
  * `case-histories`, `followup-notes`, `physical-generals`, `mental-generals`, `examinations`, `diagnoses`
  * `investigations`, `investigation-reports`
  * `past-treatments`, `past-treatment-attachments`
  * `patient-images`
  * `repertory-analyses`
* **Prescriptions:**
  * `prescriptions` (editing re-sends to the pharmacy if the prescription was already sent)
  * `POST /api/prescriptions/<id>/send-to-pharmacy/` with `{"keep_open": false}`
* **Follow-ups:**
  * `followups` (progress notes)
  * `scheduled-followups`, with actions `reschedule`, `cancel` and `check-in`
* **Pharmacy:** `dispense-orders`, with actions `start-preparing`, `mark-ready`, `mark-dispensed` and `cancel`. Responses contain items only, never clinical data.
* **Notifications:**
  * `GET /api/notifications/unread/`
  * `POST /api/notifications/<id>/read/`
  * `POST /api/notifications/read-all/`
* **Admin:** `users`, `audit-logs`.
* **Dashboard:** `dashboard` (role-filtered).
* **Query features:**
  * Pagination: `?page=`
  * Search: `?search=`
  * Sorting: `?ordering=`
  * Filtering, e.g. `?status=SCHEDULED&scheduled_date__lte=2026-11-01`

## Data model and audit

Every clinical table carries `created_at`, `created_by`, `updated_at`, `updated_by`, `deleted_at`, `deleted_by` and `is_deleted`. Deletes are soft.

Every create, update and delete is written to `audit_logs` with a field-level diff, user and IP address. The log also records workflow actions:

* views, logins and lockouts, exports
* check-in, visit type changes
* follow-ups: scheduled, rescheduled, cancelled, completed, missed
* prescriptions: repeated, sent, re-sent, updated
* patient saved without a case sheet
* relative linked, edited or removed; family history inserted
* every dispense status change; label printed

```
users ─┬─< visits (doctor)                                   audit_logs >── users
       └─< notifications (recipient)
patients ─┬─< visits ──┬── case_histories / physical_generals / mental_generals (1:1)
          │            ├── followup_notes / examinations / diagnoses / past_treatments (1:1)
          │            ├── repertory_analysis (1:1) ──< repertory_entries
          │            ├──< investigations ──< investigation_reports
          │            ├──< prescriptions ──< prescription_items
          │            │          └──< dispense_orders (versioned) ──< dispense_items (snapshot)
          │            ├──< followups (progress notes)
          │            └── previous_visit ──> visits (follow-up chain)
          ├──< scheduled_followups (one SCHEDULED per patient)
          ├──< patient_images
          └──< patient_relationships >── patients (stored both ways)
opd_counters  (per-year OPD sequence)
```

**Migrations:**

| Migration | What it does |
|---|---|
| `clinic/0002_checkin_and_followup_scheduling` | Visit `status` and `previous_visit`; follow-up notes; scheduled follow-ups |
| `clinic/0003_backfill_visit_status_and_followups` | Fills in data for existing visits: links follow-ups to their previous visit, sets status (past visits completed), and turns future follow-up dates into `SCHEDULED` records |
| `clinic/0004_relationships_and_pharmacy` | Relationships; prescription item form, quantity and dose; dispense orders and items |
| `clinic/0005_family_inference_prefill_lab` | Links get `source`, `inferred_via`, `status` (existing links become `MANUAL`/`ACTIVE`). Visits get `consultation_mode` (existing visits become `IN_PERSON`) and `reported_by`. History sections get `prefill_sources`. Dispense orders get `collect_by`. Investigations get `completed_at`. Reports get `remarks`, `version`, `status`, `supersedes` |
| `clinic/0006_backfill_investigation_completed_at` | Sets `completed_at` for investigations that were already completed |
| `core/0004` | New audit actions and notification kinds |
| `core/0002` | Audit action choices |
| `core/0003` | Audit action choices; notifications |

After upgrading, apply the migrations and build the automatic family links once:

```bash
python manage.py migrate
python manage.py rebuild_family_links
```

## Built in for later

* **SMS / WhatsApp:** `core/notifications.py` has a backend interface (the console backend just logs) and an `SMS_ENABLED` / `WHATSAPP_ENABLED` switch. The `send_followup_reminders` command and its CronJob already call it. To go live, plug in a provider such as Twilio, Gupshup or the Meta Cloud API.
* **PDF prescriptions:** the print view is laid out for A4 and saves to PDF from the browser. A server-side renderer such as WeasyPrint can render the same template.
* **Electronic signature:** each user has a signature image (set on the user edit screen), which is printed on prescriptions.

## Project layout

```
hamsa/      settings, urls, wsgi
core/       audited base model + soft delete, audit log, notifications (inbox.py), request-context middleware,
            upload validators, dashboard, protected media, management commands
accounts/   custom User (role, lock state, signature), RBAC table, auth backend, user admin
clinic/     clinical models, case-record form bundle, views, reports, tests
            services.py (check-in, follow-ups, repeat), history.py (doctor's history),
            relationships.py + family.py (family links/history), pharmacy.py (dispense orders)
api/        DRF serializers, viewsets, permissions, Swagger
templates/  Bootstrap 5 screens      static/  css, js, logo
k8s/        Kubernetes manifests     docker/  entrypoint
```
