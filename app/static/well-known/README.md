Put `assetlinks.json` here to ship the app as an Android TWA on the Play Store.

Bubblewrap generates it (`bubblewrap init` prints it, or take the SHA-256 signing
fingerprint from the Play Console → Setup → App signing). It is served at
`/.well-known/assetlinks.json`, which is where Android looks for it.

Without this file the Android wrapper shows a browser address bar instead of
running full-screen. It is not needed for the installable web app.
