# 🛡️ NovaShields Backend — Python FastAPI Server

This directory contains the FastAPI-based backend and Machine Learning classification engine for the NovaShields Smart Black Box platform. It has been separated into its own folder so that you can deploy it easily to **Render**.

---

## 🚀 Deployment Guide for Render

To deploy this backend, follow these step-by-step instructions.

### Step 1: Push this folder to a new GitHub Repository

Since Render deploys directly from GitHub, you need to publish this folder as its own GitHub repository.

1. Open a terminal inside the **`backend`** folder (`c:/Users/gmaya/OneDrive/Desktop/Nova_APP-main/backend`).
2. Verify that Git is initialized and add all files:
   ```bash
   git status
   git add .
   git commit -m "Initial backend package commit"
   ```
3. Go to [GitHub](https://github.com) and create a new **public** or **private** repository named `novashields-backend` (do not initialize it with a README or gitignore).
4. Copy the remote URL and run:
   ```bash
   git branch -M main
   git remote add origin <your-copied-github-repo-url>
   git push -u origin main
   ```

---

### Step 2: Deploy on Render

#### Option A: One-Click Blueprint Deployment (Recommended)
This repository includes a `render.yaml` file that allows Render to automatically configure all settings.

1. Log in to [Render Dashboard](https://dashboard.render.com).
2. Click the **New +** button in the top right, and select **Blueprint**.
3. Connect your GitHub account and select your `novashields-backend` repository.
4. Render will read the `render.yaml` file and show a list of settings. Fill in the requested **Environment Variables**:
   * `MONGO_URL`: Set this to your MongoDB connection string (e.g. `mongodb+srv://...`) or set to `mock` to run a mock local database.
   * `EMERGENT_LLM_KEY`: Enter your Claude API Key (optional).
   * `FIREBASE_SERVICE_ACCOUNT_PATH`: Enter your Firebase service account JSON path (optional).
5. Click **Apply** to deploy the service.

#### Option B: Manual Web Service Deployment
If you prefer to configure it manually:

1. Click **New +** and select **Web Service**.
2. Select your `novashields-backend` repository.
3. Configure the following settings:
   * **Name**: `novashields-backend`
   * **Runtime**: `Python 3`
   * **Build Command**: `pip install -r requirements.txt`
   * **Start Command**: `uvicorn server:app --host 0.0.0.0 --port $PORT`
4. Expand the **Advanced** section and add the following **Environment Variables**:
   * `MONGO_URL` = `mock` (or your MongoDB Atlas connection string)
   * `DB_NAME` = `novashields`
   * `CORS_ORIGINS` = `*`
   * `EMERGENT_LLM_KEY` = `your-claude-api-key` (optional)
5. Click **Create Web Service**.

---

### Step 3: Connect Frontend and Mobile Clients

Once the deploy completes successfully, Render will provide a public URL (e.g. `https://novashields-backend.onrender.com`). Use this URL to update your clients:

#### 1. React Frontend PWA
* Open the `frontend` directory.
* Open or create a `.env` file.
* Update `REACT_APP_BACKEND_URL`:
  ```env
  REACT_APP_BACKEND_URL=https://novashields-backend.onrender.com
  ```

#### 2. Flutter Mobile Application
* Open `novashield_mobile/lib/services/api_service.dart`.
* Locate the static variable `baseUrl` (around line 10):
  ```dart
  static String baseUrl = "https://novashields-backend.onrender.com";
  ```

---

## 💻 Local Development Setup

To run the backend locally, you can follow these steps:

1. Create a virtual environment and activate it:
   ```bash
   python -m venv venv
   # On Windows:
   venv\Scripts\activate
   # On macOS/Linux:
   source venv/bin/activate
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your values.
4. Run the FastAPI development server:
   ```bash
   uvicorn server:app --reload --port 8000
   ```
5. Access the API documentation at: `http://localhost:8000/docs`
