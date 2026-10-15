# Telos for Home Assistant

Brings your [Telos](https://github.com/vantreeseba/apps) projects into Home Assistant as todo lists. Each open project becomes a todo list that you can add to, check off, edit and clear from HA. Changes made in Telos show up straight away over its live board subscription.

## Install

**HACS:** add this repository as a custom repository (category *Integration*), install **Telos**, and restart Home Assistant.

**Manual:** copy `custom_components/telos` into your Home Assistant `config/custom_components/` directory and restart.

## Set up

1. *Settings → Devices & services → Add integration → Telos.*
2. Enter your Telos server URL (e.g. `http://192.168.1.10:3001`) and the email you sign in with.
3. Telos makes a magic link. It has no mail provider, so the link is printed in the **Telos server's log**. Paste the link, or just its token, into the form. If the server exposes links (outside production, or with `EXPOSE_MAGIC_LINK=true`), the field is filled in for you. With `AUTH_MAGIC_LINK=false`, step 3 is skipped.

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
