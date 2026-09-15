# On-Call APIs — cURL Reference (via unified-admin)

```
https://unifiedadmin.1mg.com/hra/hr_admin_service/v1/health-record/admin/...
```

| Layer | File |
|---|---|
| Gateway routes | `unified-admin-app/app/routes/hr_admin_service/v1/hr_admin.py` |
| Gateway → HR client | `unified-admin-app/app/clients/hr_digitisation_service_client.py` |
| HR handler | `hr_digitisation/app/routes/handlers/on_call/on_call_handler.py` |
| HR logic | `hr_digitisation/app/managers/on_call/on_call_manager.py` |

The gateway path `/health-record/admin/<x>` maps 1:1 to the HR path
`/v1/health-record/on-call/<x>`.

### Auth

Only two headers matter (plus `content-type` when there is a body):

```
authorization: Bearer <admin_bearer_token>
x-access-key: 1mg_client_access_key
```

- Requires `ROLE_SUPER_ADMIN` or `ROLE_ADMIN` on the `HR_ADMIN` service.
- Grab the token from DevTools → Network on
  `https://unifiedadmin.1mg.com/healthrecords/<order_group_id>/oncalls`
  (any request → Request Headers → `authorization`). It expires in ~10h.
- **Do not** send `X-SHARED-CONTEXT` — the gateway builds it from your token and
  that is what becomes `whodunnit` in the on-call logs.
- Cookies and browser headers (`sec-ch-ua`, `referer`, `user-agent`, …) are not
  needed.

Replace `<...>` placeholders before running.

---

## 1. Get user details for an order

**Resolves an order_group_id to its user_id and lists every patient of that user
(id, name, dob, gender, parent_id, is_default) — the usual first call to find
out who and which profile the on-call is about.**

```bash
curl --location 'https://unifiedadmin.1mg.com/hra/hr_admin_service/v1/health-record/admin/users/order/<order_group_id>' \
--header 'authorization: Bearer <admin_bearer_token>' \
--header 'x-access-key: 1mg_client_access_key'
```

---

## 2. Fetch all orders of a user

**Lists the user's diagnostic orders/bookings from Dexter, newest delivered
first, so you can spot the order and booking that need fixing.**

Drop the query param to get every order of the user (capped at 2000 bookings).

```bash
curl --location 'https://unifiedadmin.1mg.com/hra/hr_admin_service/v1/health-record/admin/user/<user_id>/orders?order_group_id=<order_group_id>' \
--header 'authorization: Bearer <admin_bearer_token>' \
--header 'x-access-key: 1mg_client_access_key'
```

---

## 3. Get diagnostic bookings + parameters

**Dumps what HR actually stored for that order and patient — the
DiagnosticBookings rows and the digitised lab parameters — to confirm whether
the data landed on the right profile.**

```bash
curl --location 'https://unifiedadmin.1mg.com/hra/hr_admin_service/v1/health-record/admin/order/<order_group_id>/bookings/diagnostic?patient_id=<patient_id>&booking_id=<booking_id>' \
--header 'authorization: Bearer <admin_bearer_token>' \
--header 'x-access-key: 1mg_client_access_key'
```

---

## 4. Get JSON report URL of a booking

**Returns a presigned link to the raw digitisation JSON dump of a booking, for
checking what the digitiser read off the report.**

```bash
curl --location 'https://unifiedadmin.1mg.com/hra/hr_admin_service/v1/health-record/admin/booking/<booking_id>/json-report?order_group_id=<order_group_id>' \
--header 'authorization: Bearer <admin_bearer_token>' \
--header 'x-access-key: 1mg_client_access_key'
```

---

## 5. Get patient versions (audit trail)

**Returns the patient record's version history with the changed fields decoded —
use it to see what a previous on-call or the user changed, and when.**

```bash
curl --location 'https://unifiedadmin.1mg.com/hra/hr_admin_service/v1/health-record/admin/patient/<patient_id>/versions' \
--header 'authorization: Bearer <admin_bearer_token>' \
--header 'x-access-key: 1mg_client_access_key'
```

---

## 6. Get presigned URL for private content

**Signs any private S3 URL (smart report PDF, report file, JSON dump) so you can
open it in a browser.** Returns the URL unchanged if it isn't private content.

`ttl` is optional (seconds).

```bash
curl --location 'https://unifiedadmin.1mg.com/hra/hr_admin_service/v1/health-record/admin/presigned-url' \
--header 'authorization: Bearer <admin_bearer_token>' \
--header 'x-access-key: 1mg_client_access_key' \
--header 'content-type: application/json' \
--data '{
    "url": "https://1mg-droplet-production-internal.s3.ap-south-1.amazonaws.com/upload/reports/<id>/report-<hash>.pdf",
    "ttl": 3600
}'
```
