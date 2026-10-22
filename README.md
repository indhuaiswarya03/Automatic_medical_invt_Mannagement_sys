# Medical Inventory Priority Matrix and Crisis Resource Allocator

Run on Windows (PowerShell). Do the steps in order.

**STEP 1** Install Python 3 from python.org (tick "Add Python to PATH").

**STEP 2** Install MySQL (MySQL Installer for Windows). Remember the root password.

**STEP 3** Create the database (MySQL Command Line Client or Workbench):

    CREATE DATABASE medical_crisis;

**STEP 4** Open PowerShell in this project folder, then:

    python -m venv venv
    .\venv\Scripts\Activate.ps1

(If activation is blocked: `Set-ExecutionPolicy -Scope Process Bypass`)

**STEP 5** Install dependencies:

    pip install -r requirements.txt

**STEP 6** Tell the app how to reach MySQL. Replace YOUR_PASSWORD with your real MySQL root password:

    $env:DATABASE_URL="mysql+pymysql://root:YOUR_PASSWORD@localhost/medical_crisis"

If root has no password:

    $env:DATABASE_URL="mysql+pymysql://root:@localhost/medical_crisis"

**STEP 7** Create tables and demo data:

    flask --app app seed

**STEP 8** Start the app:

    python app.py

**STEP 9** Open http://127.0.0.1:5000

## Demo logins
| Role | Email | Password |
|---|---|---|
| hospital | hospital@medicrisis.local | hospital123 |
| provider | supplier@medicrisis.local | supplier123 |
| admin | admin@medicrisis.local | admin123 |

Note: Priority uses `quantity <= 50% of minimum` = CRITICAL, `<= minimum` = LOW. Oxygen (50 of 100) is therefore CRITICAL.
Offline mode: open the app once online (installs the service worker). Offline edits are queued in the browser and synced automatically when the connection returns.
Set `SECRET_KEY` as an environment variable for anything beyond local demo.
