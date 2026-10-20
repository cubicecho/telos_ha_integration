# Telos for Home Assistant

Brings your Telos projects into Home Assistant as todo lists. Each open project becomes a todo list that you can add to, check off, edit and clear from HA. Changes made in Telos show up straight away over its live board subscription.

## Install

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=cubicecho&repository=telos_ha_integration&category=integration)

**HACS:** click the button above, or add this repository as a custom repository (category *Integration*). Install **Telos**, then restart Home Assistant.

**Manual:** copy `custom_components/telos` into your Home Assistant `config/custom_components/` directory and restart.

## Set up

[![Open your Home Assistant instance and start setting up Telos.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=telos)

1. Click the button above, or go to *Settings → Devices & services → Add integration → Telos.*
2. Enter your Telos server URL (e.g. `http://192.168.1.10:3001`).
3. Pick how to sign in. Only the ways your Telos instance offers are shown.
   - **Sign in with your provider in your browser.** This is for instances that sign in through an OIDC provider such as heautou. Home Assistant shows a link and a code. Open the link, sign in, check the code matches, and approve. Home Assistant carries on by itself. Your provider account must already be linked to Telos, so sign in to Telos with it once first.
   - **Email me a sign-in link.** Enter your email, then paste the link Telos emails you (or just its token). This needs `SMTP_URL` on the Telos server. On a server running with `SECURE_LOCAL_NET`, the address alone signs you in.

Home Assistant then holds a Telos sign-in that keeps itself fresh: provider tokens are refreshed automatically, and a session stays valid while HA keeps polling. If it ever lapses, HA asks you to sign in again.

### Setting up provider sign-in (admin, once)

The provider must support the OAuth device flow and resource indicators. heautou does.

1. At the provider, register **Telos as an API**. Its identifier is Telos's audience: `OIDC_API_AUDIENCE`, which defaults to `<Telos server URL>/graphql`.
2. Register a **public client** for Home Assistant (no secret) with the *device code* and *refresh token* grants, and allow it on the Telos API.
3. On Telos, set `OIDC_DEVICE_CLIENT_ID` to that client's id.

> Why not an API key? Telos's `telos_` keys are for AI/MCP clients. They only work with AI switched on, and they only see AI-enabled projects.

## What you get

| Home Assistant | Telos |
|----------------|-------|
| Todo list | Open project |
| Item / description / due | Todo title / notes / due date |
| Check off / uncheck | Complete / reopen (moves the card to or from the done lane) |
| Remove item | Archive (restore it from the project's archive) |

Checking off a todo that still depends on an open todo fails with Telos's error message.
