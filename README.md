# Telos for Home Assistant

Brings your Telos projects into Home Assistant as todo lists. Each open project becomes a todo list that you can add to, check off, edit and clear from HA. Changes made in Telos show up straight away over its live board subscription.

## Install

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=cubicecho&repository=telos_ha_integration&category=integration)

**HACS:** click the button above, or add this repository as a custom repository (category *Integration*). Install **Telos**, then restart Home Assistant.

**Manual:** copy `custom_components/telos` into your Home Assistant `config/custom_components/` directory and restart.

## Set up

[![Open your Home Assistant instance and start setting up Telos.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=telos)

1. Click the button above, or go to *Settings → Devices & services → Add integration → Telos.*
2. Enter your Telos server URL (e.g. `http://192.168.1.10:3001`) and the email you sign in with.
3. Telos emails you a sign-in link (this needs `SMTP_URL` set on the Telos server). Paste the link, or just its token, into the form. On a server running with `SECURE_LOCAL_NET`, step 3 is skipped.

Home Assistant then holds a Telos session, which stays valid while it keeps polling. If the session ever expires, HA asks you to sign in again the same way.

> Why not an API key? Telos's `telos_` keys are for AI/MCP clients. They only work with AI switched on, and they only see AI-enabled projects.

## What you get

| Home Assistant | Telos |
|----------------|-------|
| Todo list | Open project |
| Item / description / due | Todo title / notes / due date |
| Check off / uncheck | Complete / reopen (moves the card to or from the done lane) |
| Remove item | Archive (restore it from the project's archive) |

Checking off a todo that still depends on an open todo fails with Telos's error message.
