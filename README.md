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

---

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
| `40-followup-reminders-cronjob.yaml` | Daily follow-up reminder job (SMS/WhatsApp, disabled by default) |

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

The rules live in a single table, [accounts/rbac.py](accounts/rbac.py). Both the HTML screens and the REST API use it.

| Section | Admin | Doctor | Attender | Lab |
|---|---|---|---|---|
| Patient details / photo | edit | edit | edit | view |
| Case history, Physical generals | edit | edit | view | view |
| **Mental generals** | edit | edit | hidden | hidden |
| Physical examination | edit | edit | edit | view |
| Diagnosis | edit | edit | hidden | view |
| Investigations (request) | edit | edit | edit | view |
| Investigation reports (upload) | edit | edit | upload | **upload** |
| Past treatment | edit | edit | edit | view |
| Before/after images | edit | edit | view | hidden |
| Repertory analysis | edit | edit | hidden | hidden |
| **Prescription** | edit | edit | hidden | hidden |
| Follow-up history | edit | edit | hidden | hidden |
| Delete patients / visits | ✔ | – | – | – |
| Remove uploads / grid rows | ✔ | ✔ | – | – |
| Users, audit log | ✔ | – | – | – |
| Reports | ✔ | ✔ | – | – |

* **Hidden sections are never rendered or serialised.** A section the user can only view renders with disabled inputs. On save, the server ignores any section the user cannot edit, even if the browser posts data for it.
* **Default expansion when a record opens:**
  * Doctor and Admin: Patient, Case history, **Mental generals**, Diagnosis and **Prescription** are open.
  * Attender: Patient, Examination and Investigations are open.
  * Lab: Patient and Investigations are open.
  * Every section can be expanded or collapsed. The sticky bar at the bottom has *Expand all* and *Collapse all*.
* **Lab uploads:** a lab assistant's upload is tagged `LAB` and marks the investigation *Completed*. It then drops off the dashboard's *Pending lab reports* list.
* **Uploaded files:** files are served only through an authenticated view that applies the same section rules. A lab user cannot fetch a repertory sheet by guessing its URL, and file names are random UUIDs.

## Main screens

| Screen | URL |
|---|---|
| Home: launcher tiles (Dashboard, New patient, Add follow-up, Find patient, Reports) and widgets (today's patients, upcoming follow-ups, pending lab reports, recent prescriptions) | `/` |
| Find patient (search by OPD No., name or mobile) | `/patients/` |
| Add follow-up (search → start follow-up visit; the previous visit's case history and generals are carried forward) | `/patients/?mode=followup` |
| Register patient (OPD No. is generated automatically as `OPD-YYYY-NNNNN`) | `/patients/new/` |
| Case record for a visit. A collapsible *Previous visit history* grid sits at the top; clicking a row opens a modal with that visit's case history, generals, diagnosis, prescription, investigations and follow-up notes, filtered by role | `/visits/<id>/` |
| Printable prescription (print or save as PDF; uses the doctor's signature image if uploaded) | `/visits/<id>/prescription/` |
| Reports with CSV export | `/reports/` |
| User management (create, edit, reset password, lock/unlock, delete) | `/accounts/users/` |
| Audit log | `/audit/` |
| Swagger UI / ReDoc / OpenAPI schema | `/api/docs/`, `/api/redoc/`, `/api/schema/` |

**Repertory chart.** Enter remedies against each rubric as `Nux-v:3, Sulph:2`. *Generate Chart* builds the repertorisation grid: the top 8 remedies, colour-coded by grade. You can sort it by weighted total (remedy grade × rubric grade) or by the number of rubrics covered.

## REST API

* **Authentication:** session login, or a token:
  * `POST /api/auth/login/ {"username","password"}` returns `{"token": …}`.
  * Send the token as `Authorization: Token <key>`.
  * `GET /api/auth/me/` returns the current user's role and per-section access.
* **Resources:**
  * `patients`, `visits` (the detail view nests every section the caller may see)
  * `case-histories`, `physical-generals`, `mental-generals`, `examinations`, `diagnoses`
  * `investigations`, `investigation-reports` (multipart `file`)
  * `past-treatments`, `past-treatment-attachments`
  * `patient-images`
  * `repertory-analyses` (nested `entries`)
  * `prescriptions` (nested writable `items`)
  * `followups`
  * `users` (admin; actions `lock`, `unlock`, `reset-password`)
  * `audit-logs` (admin, read-only)
  * `dashboard`
* **Query features:**
  * Pagination: `?page=`
  * Search: `?search=`
  * Sorting: `?ordering=-visit_date`
  * Filtering, e.g. `?visit__patient=3&status=REQUESTED` or `?visit_date__gte=2026-01-01`

## Data model and audit

Every clinical table carries `created_at`, `created_by`, `updated_at`, `updated_by`, `deleted_at`, `deleted_by` and `is_deleted`. Deletes are **soft**: the row is hidden everywhere but kept. Deleting a patient also soft-deletes that patient's visits.

Every create, update and delete is written to `audit_logs` with a field-level old/new diff, the user and their IP address. The audit log also records:

* record views
* logins and logouts
* failed logins
* lock/unlock events
* password resets
* exports

After 5 failed logins (`MAX_FAILED_LOGINS`) an account locks until an administrator unlocks it. Sessions expire after 8 hours or when the browser closes, and CSRF protection is on for every form.

```
users ─┬─< visits (doctor)                       audit_logs >── users
       │
patients ──< visits ──┬── case_histories        (1:1)
           │          ├── physical_generals     (1:1)
           │          ├── mental_generals       (1:1)
           │          ├── examinations          (1:1)
           │          ├── diagnoses             (1:1)
           │          ├── past_treatments (1:1) ──< past_treatment_attachments
           │          ├── repertory_analysis (1:1) ──< repertory_entries
           │          ├──< investigations ──< investigation_reports
           │          ├──< prescriptions ──< prescription_items
           │          └──< followups
           └──< patient_images (stage BEFORE/AFTER, optional visit)
opd_counters  (per-year sequence for OPD numbers, row-locked)
```

Roles are stored on `users.role` (`ADMIN`, `DOCTOR`, `ATTENDER`, `LAB`).

To print the full PostgreSQL DDL, connect to a database and run `python manage.py sqlmigrate clinic 0001`. Do the same for `accounts` and `core`.

## Built in for later

* **SMS / WhatsApp:** `core/notifications.py` has a backend interface (the console backend just logs) and an `SMS_ENABLED` / `WHATSAPP_ENABLED` switch. The `send_followup_reminders` command and its CronJob already call it. To go live, plug in a provider such as Twilio, Gupshup or the Meta Cloud API.
* **PDF prescriptions:** the print view is laid out for A4 and saves to PDF from the browser. A server-side renderer such as WeasyPrint can render the same template.
* **Electronic signature:** each user has a signature image (set on the user edit screen), which is printed on prescriptions.

## Project layout

```
hamsa/      settings, urls, wsgi
core/       audited base model + soft delete, audit log, request-context middleware,
            upload validators, dashboard, protected media, management commands
accounts/   custom User (role, lock state, signature), RBAC table, auth backend, user admin
clinic/     clinical models, case-record form bundle, views, reports, tests
api/        DRF serializers, viewsets, permissions, Swagger
templates/  Bootstrap 5 screens      static/  css, js, logo
k8s/        Kubernetes manifests     docker/  entrypoint
```
