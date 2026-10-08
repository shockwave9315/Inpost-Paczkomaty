# 📦 InPost Paczkomaty - Home Assistant Integration

Track [InPost](https://inpost.pl/) parcels sent to a *Paczkomat®* (parcel locker) and monitor the occupancy of your
configured lockers.

> **Note:** The integration only tracks **en route** or **available for pickup** parcels. Parcels that have already been
> picked up or are otherwise delivered are ignored.

---

> **Upgrading to 0.5.0**
>
> * Requires Home Assistant **2026.10** or newer. Older releases are not supported.
> * An account is now identified by its **full phone number, including the country prefix** (for example
>   `+48123456789`), because the national number alone is not unique. An existing entry learns its full number from
>   your InPost profile the first time it starts after the upgrade; nothing has to be entered again.
> * Entities are re-created under a new internal identity. Entity IDs follow the scheme documented
>   [below](#entities) and now contain the full number: `sensor.inpost_123456789_...` becomes
>   `sensor.inpost_48123456789_...`, the per-locker IDs become `sensor.inpost_48123456789_<locker>_...`, and the two
>   binary sensors are now named `..._parcels_en_route` and `..._ready_for_pickup`. Update dashboards and automations
>   that reference the old IDs. The config entry, tokens and selected lockers are kept - there is no need to remove
>   the integration.
> * If InPost stops accepting the stored login, Home Assistant now asks you to **re-authenticate** instead of
>   requiring the integration to be removed and added again.

## How It Works

1. **Authentication:** During setup, Home Assistant shows a link to the **InPost login page**. You open it in your
   browser and sign in there - handling the **phone number**, **SMS code**, **captcha** and (if prompted) **email
   confirmation** on InPost's own pages. After a successful login your browser is redirected to a
   `https://account.inpost-group.com/callback?code=...` page; you copy that address (or just the code) and paste it back
   into Home Assistant.
   > **Tip:** If opening the InPost login page shows you as **already logged in/empty page** (it skips straight past the sign-in),
   > clear your browser cookies for `account.inpost-group.com` and open the link again to complete a fresh login.
2. **Data Flow:** Login happens entirely in your browser on official InPost pages. Home Assistant only exchanges the
   returned authorization code for API tokens (access token, refresh token, etc.), which are stored locally on your HA
   instance.
3. **Polling:** Home Assistant polls the InPost API every **30 seconds** (configurable) to retrieve the latest updates on your
   parcels. When a request fails, the interval doubles after each consecutive failure (up to one hour) and returns to
   the configured value after the first success. An HTTP 429 response is honoured, including its `Retry-After` delay.
4. **Token refresh:** The access token is renewed automatically and the new tokens are saved, so they survive a
   restart. If InPost rejects the refresh token, a **re-authentication** request appears in *Settings → Devices &
   Services*; signing in again restores the existing entry with all its entities.

---

## Installation

### HACS (Recommended)

1. Ensure **HACS** (Home Assistant Community Store) is installed.
2. Go to HACS, select **Integrations**, and click the **three-dot menu** $\rightarrow$ **Custom repositories**.
3. Add this integration's repository URL (if it's not already in the default HACS list).
4. Search for and install the **InPost Paczkomaty** integration.
5. **Restart Home Assistant**.
6. Go to **Settings** $\rightarrow$ **Devices & Services** $\rightarrow$ **Integrations** $\rightarrow$ **Add
   Integration**, and search for **InPost Paczkomaty**.
7. Open the **InPost login page** link shown in the setup dialog and sign in in your browser (phone number, SMS code, captcha and, if prompted, email confirmation). **Note:** Any verification email from InPost is legitimate - it will **not** ask for any credentials. If the page shows you as **already logged in/empty page**, clear your browser cookies for `account.inpost-group.com` and open the link again.
8. After logging in, your browser is redirected to a `https://account.inpost-group.com/callback?code=...` page (it may look blank or show an error - that is fine). Copy the full address from your browser's address bar and paste it back into Home Assistant.
9. Select the parcel lockers you wish to monitor. Your favorite lockers from your InPost profile will be pre-selected automatically. The dropdown lists the 300 lockers nearest to your Home Assistant home location; to add any other locker, type its code (for example `GDA117M`) and press Enter. A code that is not on InPost's public list is flagged once - submit again to add it anyway (the public list can lag behind newly opened lockers).

Each InPost account can be added once. To track parcels of several people, add each account separately. An account
is its full phone number including the country prefix, so `+48 123 456 789` and `+380 123 456 789` are two accounts.

> 🎥 Prefer to watch?
> 
> [![authentication tutorial on YouTube](https://img.youtube.com/vi/C_7XYLEjjgs/0.jpg)](https://www.youtube.com/watch?v=C_7XYLEjjgs).

### Manual Installation

1. Download the source code archive of the latest release (or of the `master` branch) from GitHub.
2. Unpack it and copy the `custom_components/inpost_paczkomaty` directory into the `custom_components` directory within
   your Home Assistant configuration folder.
3. **Restart Home Assistant**.
4. Execute steps **6, 7, 8, and 9** from the HACS installation method above.

---

## Configuration

The integration can be customized via `configuration.yaml`. All options are **optional** and have sensible defaults.
They apply to **all** configured accounts and are read at startup, so restart Home Assistant after changing them. The
tracked parcel lockers are changed in the UI: *Settings → Devices & Services → InPost Paczkomaty → Configure*.

```yaml
inpost_paczkomaty:
  update_interval_seconds: 30
  http_timeout_seconds: 30
  ignored_en_route_statuses:
    - CONFIRMED
  show_only_own_parcels: false
  parcel_lockers_url: "https://inpost.pl/sites/default/files/points.json"
```

### Configuration Options

| Option | Type | Default | Description |
|:-------|:-----|:--------|:------------|
| `update_interval_seconds` | integer, at least 1 | `30` | How often (in seconds) the integration polls the InPost API for updates. This is an unofficial API of the InPost mobile app; if you do not need near-real-time updates, a larger value (for example `300`) is kinder to it. |
| `http_timeout_seconds` | integer, at least 1 | `30` | HTTP request timeout in seconds. Increase if you experience timeout errors. |
| `ignored_en_route_statuses` | list | `["CONFIRMED"]` | List of en route statuses to leave out of the "en route" counts and list. See [Parcel Statuses](#parcel-statuses) below. |
| `show_only_own_parcels` | boolean | `false` | When `true`, only shows parcels you own - in every sensor, including the all parcels count. When `false`, also shows parcels shared with you by others (e.g., family members). Useful to avoid duplicate counting in multi-user households. |
| `parcel_lockers_url` | url | [InPost points URL](https://inpost.pl/sites/default/files/points.json) | URL for fetching the parcel lockers list. Only change if InPost changes their endpoint or if you want to use custom parcel lockers list. |

### Parcel Statuses

Every status InPost publishes in its [status dictionary](https://api-shipx-pl.easypack24.net/v1/statuses) belongs to
one of three groups. The group decides which sensors a parcel shows up in:

| Group | Meaning | Counted as |
|:------|:--------|:-----------|
| **Ready for pickup** | The parcel waits for you and can be collected now. | "ready for pickup" counts, the `ready_for_pickup` list (with `open_code` and `qr_code`) |
| **En route** | InPost has the parcel and is not done with it: on its way, delayed, stored, re-routed or awaiting another delivery attempt. | "en route" counts, the `en_route` list |
| **Finished** | Delivered, returned to the sender, refused or cancelled. | only the all parcels count (and, for `DELIVERED`, the carbon footprint) |

**Ready for pickup:** `READY_TO_PICKUP`, `PICKUP_REMINDER_SENT`, `READY_TO_PICKUP_FROM_POK`,
`READY_TO_PICKUP_FROM_POK_REGISTERED`, `COURIER_AVIZO_IN_CUSTOMER_SERVICE_POINT`.

**En route:**

| Stage | Statuses |
|:------|:---------|
| Not handed over to InPost yet | `CREATED`, `OFFERS_PREPARED`, `OFFER_SELECTED`, `CONFIRMED` |
| On its way | `DISPATCHED_BY_SENDER`, `DISPATCHED_BY_SENDER_TO_POK`, `COLLECTED_FROM_SENDER`, `TAKEN_BY_COURIER`, `TAKEN_BY_COURIER_FROM_POK`, `ADOPTED_AT_SOURCE_BRANCH`, `SENT_FROM_SOURCE_BRANCH`, `ADOPTED_AT_SORTING_CENTER`, `SENT_FROM_SORTING_CENTER`, `ADOPTED_AT_TARGET_BRANCH`, `OUT_FOR_DELIVERY`, `OUT_FOR_DELIVERY_TO_ADDRESS` |
| Delayed, re-routed or awaiting another attempt | `PICKUP_REMINDER_SENT_ADDRESS`, `DELAY_IN_DELIVERY`, `REDIRECT_TO_BOX`, `CANCELED_REDIRECT_TO_BOX`, `READDRESSED`, `OVERSIZED`, `UNDELIVERED_WRONG_ADDRESS`, `UNDELIVERED_INCOMPLETE_ADDRESS`, `UNDELIVERED_UNKNOWN_RECEIVER`, `UNDELIVERED_COD_CASH_RECEIVER`, `UNDELIVERED_NO_MAILBOX`, `UNDELIVERED_NOT_LIVE_ADDRESS`, `AVIZO` |
| Not collected in time, outcome still open | `PICKUP_TIME_EXPIRED`, `READY_TO_PICKUP_FROM_BRANCH` |
| Stored elsewhere until the chosen locker has room | `STACK_IN_BOX_MACHINE`, `STACK_PARCEL_IN_BOX_MACHINE_PICKUP_TIME_EXPIRED`, `UNSTACK_FROM_BOX_MACHINE`, `STACK_IN_CUSTOMER_SERVICE_POINT`, `STACK_PARCEL_PICKUP_TIME_EXPIRED`, `UNSTACK_FROM_CUSTOMER_SERVICE_POINT` |

**Finished:** `DELIVERED`, `RETURN_PICKUP_CONFIRMATION_TO_SENDER`, `CLAIMED`, `REJECTED_BY_RECEIVER`,
`RETURNED_TO_SENDER`, `TAKEN_BY_COURIER_FROM_CUSTOMER_SERVICE_POINT`, `UNDELIVERED`,
`UNDELIVERED_LACK_OF_ACCESS_LETTERBOX`, `CANCELED`.

A status that is not on these lists - including InPost's own placeholders `OTHER` and `MISSING` - is **unknown**. An
unknown parcel is never dropped silently: it stays in the all parcels count, is counted in the `unknown_parcels_count`
attribute of the parcels list sensor (its status is listed in `unknown_statuses`), and the status is written to the log
once. Please report such a status in an issue.

By default, `CONFIRMED` is left out of the en route counts because parcels in this status are often just created but
not yet physically handed over to InPost. Any en route status can be listed in `ignored_en_route_statuses`.

**Example:** To ignore both `CONFIRMED` and `DISPATCHED_BY_SENDER`:

```yaml
inpost_paczkomaty:
  ignored_en_route_statuses:
    - CONFIRMED
    - DISPATCHED_BY_SENDER
```

**Example:** To show all en route statuses (don't ignore any):

```yaml
inpost_paczkomaty:
  ignored_en_route_statuses: []
```

### Multi-User Households

If multiple family members are configured in HA and share parcels with each other, you might see duplicate parcel counts. Use `show_only_own_parcels: true` to only count parcels that belong to InPost account owner:

```yaml
inpost_paczkomaty:
  show_only_own_parcels: true
```

---

## Usage Examples

### Dashboard panel

Display parcel counts directly on your Home Assistant dashboard to see at a glance how many packages are waiting for pickup. This is especially handy if you have a dashboard near your door—check whether a trip to the Paczkomat® is needed before heading out.

**Markdown panel example:**

![Markdown panel example](docs/img/markdown-panel-example.png)

```text
# 📦 Parcels waiting: {{ (states('sensor.inpost_48123456789_ready_for_pickup_parcels_count') | int) + (states('sensor.inpost_48987654321_ready_for_pickup_parcels_count') | int) }}
## 🙋‍♀️ Wife: {{ states('sensor.inpost_48987654321_ready_for_pickup_parcels_count') }}
## 🙋‍♂️ Husband: {{ states('sensor.inpost_48123456789_ready_for_pickup_parcels_count') }}
```

#### Advanced Parcels Dashboard with QR Codes

Display detailed parcels information with QR codes for easy pickup using Home Assistant's built-in `<ha-qr-code>` component.

![Parcel dashboacd card](docs/img/parcel-dashboard-example.png)

**Markdown card configuration:**

```yaml
type: markdown
content: |
  {% set parcels_sensor = 'sensor.inpost_48123456789_parcels_list' %}
  {% set ready = state_attr(parcels_sensor, 'ready_for_pickup') or [] %}
  {% set en_route = state_attr(parcels_sensor, 'en_route') or [] %}

  # 📦 Parcels Dashboard

  ## 🟢 Ready for Pickup ({{ ready | length }})

  {% for p in ready %}
  ---
  **{{ p.sender_name or 'Unknown sender' }}** {% if p.parcel_size %}({{ p.parcel_size }}){% endif %}

  📍 **{{ p.pickup_point_name or 'Courier' }}** {% if p.pickup_point_description %}- {{ p.pickup_point_description }}{% endif %}

  {% if p.pickup_point_address %}{{ p.pickup_point_address }}{% endif %}

  {% if p.phone_number %}📱 Phone: **{{ p.phone_number }}**{% endif %}

  {% if p.open_code %}🔑 Code: **{{ p.open_code }}**{% endif %}

  {% if p.qr_code %}<ha-qr-code data="{{ p.qr_code }}" width="150"></ha-qr-code>{% endif %}

  {% endfor %}

  {% if en_route | length > 0 %}
  ## 🚚 En Route ({{ en_route | length }})

  {% for p in en_route %}
  ---
  **{{ p.sender_name or 'Unknown sender' }}** {% if p.parcel_size %}({{ p.parcel_size }}){% endif %}

  📍 {{ p.pickup_point_name or 'Courier delivery' }} {% if p.pickup_point_description %}- {{ p.pickup_point_description }}{% endif %}

  {% if p.phone_number %}📱 Phone: **{{ p.phone_number }}**{% endif %}

  Status: {{ p.status_description }}

  {% endfor %}
  {% endif %}
```

**Simpler version without QR codes:**

```yaml
type: markdown
content: |
  {% set parcels_sensor = 'sensor.inpost_48123456789_parcels_list' %}
  {% set ready = state_attr(parcels_sensor, 'ready_for_pickup') or [] %}
  {% set en_route = state_attr(parcels_sensor, 'en_route') or [] %}

  # 📦 Parcels: {{ ready | length }} ready, {{ en_route | length }} en route

  {% for p in ready %}
  ---
  ## 🟢 {{ p.sender_name or 'Unknown' }}
  📍 {{ p.pickup_point_name }} | 🔑 **{{ p.open_code }}**
  {% endfor %}

  {% for p in en_route %}
  ---
  ## 🚚 {{ p.sender_name or 'Unknown' }}
  📍 {{ p.pickup_point_name or 'Courier' }} | {{ p.status_description }}
  {% endfor %}
```

#### Parcels ready for pick up notification

Get a notification on your phone when you're approaching home and parcels are waiting at the Paczkomat®. This way, you can stop by the locker on your way in - no need to get home first, only to remember that you or someone else in your household has a package to collect.

```yaml
alias: Parcel pickup reminder
description: ""
triggers:
  - trigger: zone
    entity_id: person.husband
    zone: zone.home
    event: enter
conditions:
  - condition: or
    conditions:
      - condition: numeric_state
        entity_id: sensor.inpost_48123456789_ready_for_pickup_parcels_count
        above: 0
      - condition: numeric_state
        entity_id: sensor.inpost_48987654321_ready_for_pickup_parcels_count
        above: 0
actions:
  - action: notify.mobile_app_iphone_husband
    metadata: {}
    data:
      title: 📦 Parcels waiting
      message: >-
        🙋‍♀️ Wife: {{
        states('sensor.inpost_48987654321_ready_for_pickup_parcels_count') }}.

        🙋‍♂️ Husband: {{
        states('sensor.inpost_48123456789_ready_for_pickup_parcels_count') }}.
mode: single
```

### Carbon Footprint Visualization

The integration tracks CO₂ emissions of your delivered parcels. Carbon footprint sensors have `state_class` attributes, so Home Assistant automatically records long-term statistics.

![Carbon Footprint card](docs/img/ha-inpost-carbon-footprint-chart.png)

```yaml
type: statistics-graph
title: Carbon Footprint Over Time
entities:
  - sensor.inpost_48123456789_total_carbon_footprint
stat_types:
  - state
days_to_show: 180
```

<details>
<summary>Advanced visualization with ApexCharts Card</summary>

Install [ApexCharts Card](https://github.com/RomRider/apexcharts-card) from HACS for more advanced charts.

**Daily bar chart using sensor attributes:**

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Daily Carbon Footprint (kg CO₂)
graph_span: 30d
series:
  - entity: sensor.inpost_48123456789_carbon_footprint_statistics
    name: Daily CO₂
    type: column
    data_generator: |
      return entity.attributes.daily_data.map(d => {
        return [new Date(d.date).getTime(), d.value];
      });
```

**Using long-term statistics (recommended):**

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Carbon Footprint (Long-Term Statistics)
graph_span: 6mo
series:
  - entity: sensor.inpost_48123456789_total_carbon_footprint
    name: Total CO₂
    type: line
    statistics:
      type: state
      period: day
```

</details>

## Entities

The integration creates entities for the overall account (phone number registered in InPost mobile app) and for each tracked parcel locker.
Account entities belong to an `InPost [PHONE_NUMBER]` device and every tracked locker gets its own
`InPost [PHONE_NUMBER] [LOCKER_ID]` device, where `[PHONE_NUMBER]` is the full number with its country prefix (for
example `+48123456789`; in entity IDs the `+` is dropped: `48123456789`). The entity IDs below are the ones Home
Assistant generates for a new installation; a locker removed in the options has its device and entities removed as
well.

### Summary Entities

| Platform | Entity                                                 | Description                                                                                              |
|:---------|:-------------------------------------------------------|:---------------------------------------------------------------------------------------------------------|
| `sensor` | `inpost_[PHONE_NUMBER]_all_parcels_count`              | Total number of all tracked parcels bound to your phone number, whatever their status.                   |
| `sensor` | `inpost_[PHONE_NUMBER]_en_route_parcels_count`         | Number of parcels [en route](#parcel-statuses) to any destination.                                       |
| `sensor` | `inpost_[PHONE_NUMBER]_ready_for_pickup_parcels_count` | Number of parcels [ready for pickup](#parcel-statuses) at any destination.                               |
| `sensor` | `inpost_[PHONE_NUMBER]_parcels_list`                   | Parcels list sensor with detailed parcel data for dashboard display (see attributes below).              |

#### Parcels List Sensor Attributes

The `parcels_list` sensor provides detailed parcel information for advanced dashboard cards:

| Attribute                | Type  | Description                                                              |
|:-------------------------|:------|:-------------------------------------------------------------------------|
| `ready_for_pickup`       | list  | List of parcels ready for pickup with open codes and QR codes.           |
| `en_route`               | list  | List of parcels in transit.                                              |
| `ready_for_pickup_count` | int   | Number of parcels ready for pickup.                                      |
| `en_route_count`         | int   | Number of parcels en route.                                              |
| `invalid_parcels_count`  | int   | Parcels skipped because InPost returned them in an unexpected format (details in the log). |
| `unknown_parcels_count`  | int   | Parcels in a [status the integration does not know](#parcel-statuses); they are in neither list. |
| `unknown_statuses`       | list  | The statuses of those parcels.                                           |
| `has_more`               | bool  | `true` if InPost reported more tracked parcels than it returned; those are not shown.     |

> **Privacy:** the `ready_for_pickup` and `en_route` lists (which contain pickup codes, QR payloads and phone numbers)
> are available in the current state for dashboards and templates, but are **not** written to the recorder database.
> The same applies to `daily_data` and `cumulative_data` of the carbon footprint statistics sensor. Entity IDs contain
> the account phone number.

Each parcel in the list contains:

| Field                      | Description                                                |
|:---------------------------|:-----------------------------------------------------------|
| `shipment_number`          | Parcel tracking number.                                    |
| `sender_name`              | Name of the sender.                                        |
| `status`                   | Current parcel status.                                     |
| `status_description`       | Human-readable status description.                         |
| `shipment_type`            | Type: "parcel" (locker) or "courier".                      |
| `parcel_size`              | Size: A, B, C, or OTHER.                                   |
| `phone_number`             | Receiver phone number (e.g., "+48987654321").              |
| `pickup_point_name`        | Locker code (e.g., "GDA117M") or null for courier.         |
| `pickup_point_address`     | Formatted address of pickup point.                         |
| `pickup_point_description` | Location description (e.g., "obiekt mieszkalny").          |
| `pickup_point_city`        | City of pickup point (e.g., "Gdańsk").                     |
| `pickup_point_street`      | Street name of pickup point (e.g., "Wieżycka").            |
| `pickup_point_building`    | Building number of pickup point (e.g., "8").               |
| `pickup_point_post_code`   | Postal code of pickup point (e.g., "80-180").              |
| `open_code`                | Code to open the locker (when InPost provides one).        |
| `qr_code`                  | QR code data string (when InPost provides one).            |
| `stored_date`              | When parcel was stored in locker (ISO format).             |

### Carbon Footprint Entities

| Platform | Entity                                                      | Description                                                                                     |
|:---------|:------------------------------------------------------------|:------------------------------------------------------------------------------------------------|
| `sensor` | `inpost_[PHONE_NUMBER]_total_carbon_footprint`              | Total cumulative CO₂ in kg from all delivered parcels.                                          |
| `sensor` | `inpost_[PHONE_NUMBER]_today_carbon_footprint`              | CO₂ in kg from parcels picked up today.                                                         |
| `sensor` | `inpost_[PHONE_NUMBER]_carbon_footprint_statistics`         | Statistics sensor with daily breakdown data for graph visualization (see attributes below).     |

#### Carbon Footprint Statistics Attributes

The `carbon_footprint_statistics` sensor provides the following attributes for advanced visualization:

| Attribute         | Type  | Description                                                              |
|:------------------|:------|:-------------------------------------------------------------------------|
| `daily_data`      | list  | List of `{date, value, parcel_count}` objects for daily CO₂ graphs.      |
| `cumulative_data` | list  | List of `{date, value}` objects for cumulative CO₂ graphs.               |
| `total_co2_kg`    | float | Total carbon footprint in kilograms.                                     |
| `total_parcels`   | int   | Total number of delivered parcels counted.                               |

> **How Carbon Footprint is Calculated:**
> - Only **DELIVERED** parcels are counted
> - Uses `boxMachineDelivery` value (lower CO₂) if parcel was picked up from a **parcel locker**
> - Uses `addressDelivery` value (higher CO₂) if parcel was delivered by **courier**
> - Respects `show_only_own_parcels` configuration setting
> - Uses `pickUpDate`, converted to Home Assistant's time zone, as the date for statistics

### Per-Locker Entities

For each configured locker (identified by `[LOCKER_ID]`), the following entities are created:

| Platform        | Entity                                                     | Description                                                                        |
|:----------------|:-----------------------------------------------------------|:-----------------------------------------------------------------------------------|
| `sensor`        | `inpost_[PHONE_NUMBER]_[LOCKER_ID]_locker_id`              | The public ID of the specific parcel locker.                                       |
| `sensor`        | `inpost_[PHONE_NUMBER]_[LOCKER_ID]_description`            | Description of the locker location (e.g., "przy sklepie Biedronka"); unknown for a locker added by typing its code. |
| `sensor`        | `inpost_[PHONE_NUMBER]_[LOCKER_ID]_address`                | Address of the locker (city, zip code, street, building number), built from the parts that are known; unknown if there are none. |
| `binary_sensor` | `inpost_[PHONE_NUMBER]_[LOCKER_ID]_ready_for_pickup`       | On if **any** parcels are available for pickup in this specific locker.            |
| `sensor`        | `inpost_[PHONE_NUMBER]_[LOCKER_ID]_ready_for_pickup_count` | Number of parcels available for pickup in this specific locker.                    |
| `binary_sensor` | `inpost_[PHONE_NUMBER]_[LOCKER_ID]_parcels_en_route`       | On if **any** parcels are en route to this specific locker.                        |
| `sensor`        | `inpost_[PHONE_NUMBER]_[LOCKER_ID]_en_route_count`         | Number of parcels currently en route to this specific locker.                      |

---

## Features

* Monitor the **total** number of parcels associated with your account, whatever their status.
* Monitor the number of parcels **en route** across all destinations.
* Monitor configured lockers:
    * Count of **en route** parcels destined for the locker.
    * Count of parcels **ready for pickup** at the locker.
* Track **carbon footprint** of delivered parcels with daily and cumulative statistics.

## Troubleshooting

| Symptom | What it means / what to do |
|:--------|:---------------------------|
| "Could not read your InPost profile" during login | The login itself worked, but the profile request failed or did not contain the account's phone number with its country prefix (usually a temporary InPost problem). Choose **Try again** to repeat only that request, or **Sign in again** to start a new login. |
| A **re-authentication** request appears | InPost rejected the stored login (for example after logging out all devices). Open it, sign in again to the **same** account and paste the redirect address. Entities and settings are kept. |
| "You signed in to a different InPost account" during re-authentication | The login belongs to another phone number (the country prefix counts) than the entry. Sign in to the entry's own account, or add the other account as a new entry. |
| Entities are **unavailable** | The last update failed (InPost unreachable, rate limited, unexpected response). The integration retries on its own with an increasing delay of up to one hour; the reason is logged once when the outage starts. |
| `invalid_parcels_count` is above 0 | InPost returned a parcel the integration could not read. The other parcels are unaffected. The log names the field at fault - please include it in an issue. |
| `unknown_parcels_count` is above 0 | InPost returned a parcel in a status the integration does not know yet, so it is counted as neither ready for pickup nor en route. The status is in `unknown_statuses` and in the log - please include it in an issue. |
| `has_more` is `true` | InPost returned only part of the tracked parcels. Fetching further pages is not supported yet. |
| A locker is missing from the list | The dropdown only shows the 300 nearest lockers and InPost's public list may be out of date. Type the locker code manually. |
| "This InPost account is already configured" | The account already has an entry. Use *Configure* on it to change lockers. |
| After an upgrade the entry stays in *Retrying setup* with "Could not identify the InPost account" | An entry created before 0.5.0 reads its full phone number from the InPost profile once; that request has failed so far and is repeated automatically. |
| After an upgrade an entry fails with "already set up in another entry" | Two entries hold the same account. Remove one of them. |

To collect debug logs add the following to `configuration.yaml`. Access tokens and other credentials are masked in the
integration's log messages, but review a log before publishing it: Home Assistant itself may log entity states.

```yaml
logger:
  logs:
    custom_components.inpost_paczkomaty: debug
```

## Roadmap (in no particular order)

* Fetch further pages when InPost returns only part of the tracked parcels (`has_more`).
* Support tracking parcels sent to a parcel locker that **has not been configured** in the initial setup.
* Add a `inpost_[PHONE_NUMBER]_[LOCKER_ID]_deadline` entity to monitor pickup deadlines for each ready-for-pickup parcel in a locker.
* Add branding images to https://github.com/home-assistant/brands

Please create a new [GitHub Issue](https://github.com/shockwave9315/Inpost-Paczkomaty/issues) for any feature request you might have.

---

## Disclaimers

| Item             | Details                                                                                                                                             |
|:-----------------|:----------------------------------------------------------------------------------------------------------------------------------------------------|
| **Usage Limits** | This integration uses the unofficial API of the InPost mobile app. InPost may rate limit requests or change the API without notice.               |
| **API AUTH**     | Login requires a captcha and is completed in your browser; Home Assistant then uses the returned refresh token to keep the access token up to date. Tokens are stored in Home Assistant's config entry storage. |
| **Inspiration**  | Some parts of the codebase were **heavily** inspired by [InPost-Air](https://github.com/CyberDeer/InPost-Air).                                      |
