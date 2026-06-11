# 📅 Zoho Meetings Telegram Reminder Bot

An automated Python utility script that seamlessly integrates with your **Zoho Calendar** to fetch upcoming meeting invites, extract direct meeting links, and trigger custom alerts to your **Telegram Bot** exactly **1 hour** and **15 minutes** before the event starts. Never miss a briefing or standup again!

---

## 🚀 Features

* **Automated Sync:** Fetches real-time meeting invites from your Zoho Business Mail/Calendar.
* **Smart Extraction:** Automatically parses and extracts virtual meeting links (Zoom, Teams, Zoho Meeting, etc.).
* **Dual Reminders:** Sends precise Telegram push notifications at two critical intervals: **60 minutes** and **15 minutes** prior to the meeting.
* **Lightweight & Headless:** Can be deployed locally or hosted on a server (or via cron jobs) to run 24/7.

---

## 🛠️ Configuration & Setup

Before running the script, you need to populate the `config` section within the code with your personal credentials. Follow the guide below to gather the required secure data.

### 1. Zoho Mail Credentials
Instead of your regular account password, Zoho requires an **App-Specific Password** for third-party scripts:
1. Log in to your [Zoho Accounts](https://accounts.zoho.com/).
2. Navigate to **Security** -> **App Passwords**.
3. Click **Generate New Password**, give it a name (e.g., `TelegramBot`), and copy the generated 16-character password.
4. Update the config:
   * `ZOHO_EMAIL`: Your full Zoho business email address.
   * `ZOHO_APP_PASSWORD`: The 16-character app password you just generated.

### 2. Telegram Bot Setup
To receive reminders directly on your phone, you must create a personal Telegram bot:
1. Open Telegram and search for **[@BotFather](https://t.me/BotFather)**.
2. Send the command `/newbot` and follow the prompts to give your bot a name and username.
3. **Save the API Token** provided by BotFather (e.g., `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`).
4. To find your personal **Chat ID**:
   * Search for **[@userinfobot](https://t.me/userinfobot)** on Telegram and send it any message.
   * It will instantly reply with your unique `Id` (a string of numbers).
5. Update the config:
   * `TELEGRAM_BOT_TOKEN`: The API token from BotFather.
   * `TELEGRAM_CHAT_ID`: Your unique numerical Chat ID.

---

## 📦 Installation

1. Clone the repository:
   ```bash
   git clone [https://github.com/your-username/your-repo-name.git](https://github.com/your-username/your-repo-name.git)
   cd your-repo-name
