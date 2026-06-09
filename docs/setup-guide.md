# Kids AI - Tablet Setup Guide

This guide walks you through installing Kids AI as a Progressive Web App (PWA) on a child's tablet, configuring the endpoint, and completing the first-run setup.

## Prerequisites

- A tablet (Android, Fire HD, or iPad)
- The Kids AI server running (see [README](../README.md) for server setup)
- Your server's URL (e.g., `https://your-server.com` or `http://192.168.1.100:3000`)

## Step 1: Access the App

1. Open your tablet's browser
2. Navigate to your Kids AI server URL
3. Wait for the page to load completely

## Step 2: Install as PWA

### Android (Chrome, Samsung Internet, etc.)
1. Tap the browser menu (three dots)
2. Select **"Add to Home screen"** or **"Install app"**
3. Confirm the installation
4. The app icon will appear on your home screen

### Amazon Fire HD (Silk Browser)
1. Tap the menu icon (three lines)
2. Select **"Add to Home screen"**
3. Name the app "Kids AI"
4. Tap **"Add"**
5. The icon will appear on your home screen

### iPad (Safari)
1. Tap the Share button (square with arrow)
2. Scroll down and tap **"Add to Home Screen"**
3. Name the app "Kids AI"
4. Tap **"Add"**
5. The icon will appear on your home screen

## Step 3: Configure Endpoint

1. Open the installed Kids AI app from your home screen
2. You'll see the settings page on first launch
3. Enter your server URL in the **"Server URL"** field
4. Tap **"Save"** or **"Connect"**
5. The app will verify the connection

## Step 4: First-Run Experience

1. After successful connection, you'll see the main interface
2. The app will prompt for:
   - **Child's name** (optional, for personalization)
   - **Voice preference** (if available)
   - **Theme selection** (light/dark)
3. Complete the prompts to finish setup
4. The app is now ready for use!

## Platform-Specific Notes

### Android
- **Chrome**: Best PWA support, full offline capabilities
- **Samsung Internet**: Good support, may lack some features
- **Firefox**: Limited PWA support, use Chrome if possible

### Amazon Fire HD
- **Silk Browser**: Works well, but may have minor UI differences
- **Amazon Appstore**: Not required, PWA works directly from browser
- **Parental Controls**: Ensure "Apps from Unknown Sources" is enabled if needed

### iPad
- **Safari**: Only browser that supports PWA installation
- **Full-Screen Mode**: PWA runs in full-screen after installation
- **Notifications**: Limited support in PWA mode

## Troubleshooting

### "Cannot connect to server"
- Verify the server is running
- Check the URL is correct (include `http://` or `https://`)
- Ensure both devices are on the same network (if using local IP)

### "App won't install"
- Update your browser to the latest version
- Clear browser cache and try again
- On Fire HD, restart the tablet and retry

### "Blank screen after install"
- Close the app completely and reopen
- Check your internet connection
- Reinstall the PWA from the browser

## Screenshots

*Screenshots will be added in a future update. For now, refer to the [README](../README.md) for basic interface screenshots.*

## Additional Resources

- [Server Configuration Guide](../README.md#configuration)
- [API Documentation](../docs/api.md)
- [Parental Controls Guide](../docs/parental-controls.md)

---

*Last updated: 2024-01-15*