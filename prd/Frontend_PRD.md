# NovaShields: Frontend Product Requirement Document (PRD)

---

## 1. Project Overview
**NovaShields** is a Smart Black Box system for two-wheelers. The backend (FastAPI + MongoDB) is fully complete and functional. 

This PRD outlines the requirements for the **Frontend Application** (React.js or Next.js) so the designer/developer can build a sleek, user-friendly dashboard that seamlessly integrates with the backend API.

---

## 2. Design System & Aesthetics
* **Theme:** Dark Mode by default.
* **Style:** Modern, Sleek, Cyberpunk/Automotive aesthetic mixed with Glassmorphism (semi-transparent backgrounds, blur effects).
* **Color Palette (Suggested):** 
  * Background: Slate/Charcoal (`#0F172A`, `#020617`)
  * Accents: Emerald Green (for active/safe states), Amber/Orange (for warnings), Rose/Red (for critical crash alerts), Indigo/Blue (for generic highlights).
* **Typography:** Clean sans-serif fonts (e.g., *Inter*, *Roboto*, or *Outfit*).
* **Components:** Use Tailwind CSS or standard CSS modules. Avoid heavy UI libraries unless necessary; prefer custom styled glassmorphic cards.

---

## 3. Core Pages & Features

The frontend needs the following primary screens:

### 3.1. Authentication Screens (Login & Register)
* **Design:** Centered glassmorphic card on a dark, abstract background.
* **Fields (Register):** Name, Email, Password.
* **Fields (Login):** Email, Password.
* **Integration:** 
  * `POST /api/auth/register` (Returns `user_id`)
  * `POST /api/auth/login` (Returns JWT `access_token`). The token must be stored (localStorage/Context) and sent in the `Authorization: Bearer <token>` header for all protected API calls.

### 3.2. Main Dashboard (Overview)
* **Goal:** A quick glance at the rider's current status and recent activity.
* **Components:**
  * **Welcome Header:** Showing the user's name.
  * **Live ESP32 Camera Feed:** A card showing the latest uploaded image from the hardware. 
    * *Endpoint:* `GET /api/camera/latest/{device_id}`
  * **Quick Stats:** Total trips, recent alerts.
  * **Quick Actions:** Buttons to send remote commands to the bike (e.g., Turn on LED, Sound Buzzer).
    * *Endpoint:* `POST /api/commands` (`{ device_id, command, payload }`)

### 3.3. Trips & Rides History
* **Goal:** List and view details of past rides.
* **Components:**
  * **Trips List:** Card/Table view of all rides (showing start time, distance, top speed, max lean angle).
    * *Endpoint:* `GET /api/trips`
  * **Trip Details Modal:** Clicking a trip shows the route (if mapping is integrated), speed metrics, and any events/warnings triggered during the ride.
    * *Endpoint:* `GET /api/trips/{trip_id}`

### 3.4. Alerts & Incident Reports
* **Goal:** A log of all critical events (Hard Brakes, Bike Falls, Collisions).
* **Components:**
  * **Alerts Feed:** Showing severity (Color-coded: Info, Warning, Critical) and the AI Analysis verdict.
    * *Endpoint:* `GET /api/alerts`
  * **PDF Download:** A button on critical alerts to download the official Incident Report for insurance.
    * *Endpoint:* `GET /api/alerts/{alert_id}/report.pdf` (Opens file download directly).

### 3.5. Emergency Contacts
* **Goal:** Manage who gets notified during a crash.
* **Components:**
  * **Contacts List:** Show Name, Phone Number, Priority, and Relation.
    * *Endpoint:* `GET /api/contacts`
  * **Add Contact Form:** 
    * *Endpoint:* `POST /api/contacts`
  * **Delete Option:** Swipe or click to remove.
    * *Endpoint:* `DELETE /api/contacts/{contact_id}`

### 3.6. Medical Profile & First-Responder QR
* **Goal:** Store vital medical info that first responders can access via a QR code.
* **Components:**
  * **Editable Form:** Full Name, DOB, Blood Group, Allergies, Medications, Conditions, Organ Donor status, Insurance Provider/Policy.
    * *Endpoints:* `GET /api/medical` (Load data) and `POST /api/medical` (Save data).
  * **QR Code Display:** A generated QR code pointing to the public URL: `GET /api/public/medical/{user_id}`.

---

## 4. API Integration Guide (For Frontend Developer)

**Base URL:** Replace with your actual backend URL (e.g., `http://localhost:8000` or the Render deployed URL).

**Authentication:** 
For all endpoints (except `/auth/*` and `/public/*`), include the JWT token in the request header:
```javascript
headers: {
  'Content-Type': 'application/json',
  'Authorization': `Bearer ${localStorage.getItem('token')}`
}
```

### Essential API Dictionary

| Feature | HTTP Method | Endpoint | Request Body / Params | Notes |
| :--- | :--- | :--- | :--- | :--- |
| **Login** | POST | `/api/auth/login` | `{ email, password }` | Save `access_token` on success. |
| **Register** | POST | `/api/auth/register` | `{ email, password, name }` | Registration form. |
| **Camera Feed** | GET | `/api/camera/latest/{device_id}` | `device_id` in URL | Returns `image_url` string. |
| **Get Trips** | GET | `/api/trips` | None | Returns array of trips. |
| **Get Alerts** | GET | `/api/alerts` | `?limit=50` | Returns recent crash alerts. |
| **Get Medical** | GET | `/api/medical` | None | Rider medical data. |
| **Update Medical**| POST | `/api/medical` | `{ blood_group, allergies, ... }` | Saves the rider's profile. |
| **Contacts** | GET/POST | `/api/contacts` | `{ name, phone, relation, priority }` | CRUD for emergency contacts. |
| **Send Command** | POST | `/api/commands` | `{ device_id, command: "buzzer_on" }` | Control the bike remotely. |
| **Incident PDF** | GET | `/api/alerts/{alert_id}/report.pdf` | None (Direct URL link) | Use standard `<a href="...">` tag to trigger browser download. |

---

## 5. Development Milestones & Sprint Plan

* **Phase 1 (Foundation):** Setup React/Next.js environment, configure Tailwind CSS, create JWT Auth Context, and build Login/Register screens.
* **Phase 2 (Dashboard & Data):** Build the main dashboard layout (Sidebar/Navbar). Fetch and display the Live Camera Snapshot and Quick Stats.
* **Phase 3 (Management Pages):** Build the Trips History, Emergency Contacts, and Medical Profile edit screens.
* **Phase 4 (Polish & UI/UX):** Implement Glassmorphism styling, loading skeletons, error toasts/notifications, and test mobile responsiveness.
