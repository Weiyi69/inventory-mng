# RetailFlow

RetailFlow is a local inventory and sales management app built with Flask and SQLite.
All prices, expenses, reports, and receipts use Philippine pesos (₱).

## Run locally

1. Install Python 3.10 or newer and create/activate a virtual environment.
2. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env`, generate two different secrets, and replace
   the placeholders with distinct values (at least 32 characters each):

   ```powershell
   Copy-Item .env.example .env
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Keep `.env` private. `APP_ENV=development`, `JWT_COOKIE_SECURE=false`, and
   `RATELIMIT_STORAGE_URI=memory://` are for local development only.
4. Initialize a fresh database and create the first administrator:

   ```powershell
   python -m flask --app app db upgrade
   python -m flask --app app create-admin
   ```

   The CLI prompts for the admin name, email, and password (minimum 12
   characters). There is no public registration.
5. Start the app and open `http://127.0.0.1:5000` (anonymous visitors are
   redirected to the public catalog at `/products/`):

   ```powershell
   python app.py
   ```

### Upgrade an existing RetailFlow database

Back up `instance\inventory.db` to a safe location before migrating. The
existing application database is unversioned, so mark its verified legacy
schema before applying the additive authentication migration:

```powershell
Copy-Item instance\inventory.db "$env:USERPROFILE\inventory.db.pre-auth.bak"
python -m flask --app app db stamp 0001_legacy
python -m flask --app app db upgrade
python -m flask --app app create-admin
```

The baseline stamp does not alter tables or data. The migrations add account,
permission, audit, order, persistent-cart, and order-owner fields while keeping
product, sale, sale-item, and expense records. Do not stamp a fresh database;
use `db upgrade` for a new installation. Set `DATABASE_URL` to use another
SQLAlchemy-compatible database.

For an existing installation already migrated through authentication, back up
the database and run:

```powershell
Copy-Item instance\inventory.db "$env:USERPROFILE\inventory.db.pre-orders.bak"
python -m flask --app app db upgrade
```

### Authentication and access

- Admins can create accounts and change roles, activation status, page access,
  and individual action/data grants from **User management**.
- Standard users can access only the responsive Products catalog. They see
  active product photos (or placeholders), names, categories, and PHP prices;
  they can also maintain a persistent cart, check out with Cash on Delivery,
  and view only their own orders. They cannot access Overview, Sales, Expenses,
  Reports, or user management.
  Product creation, changes, deletion, restocking, and cost visibility remain
  unavailable unless an administrator explicitly grants the corresponding
  product action. Product stock quantities are shown on the detail page only
  when an administrator grants **View product stock quantities**.
- Unauthenticated visitors can browse the public active-product catalog and
  detail pages, add available items to a session cart, or use Buy It Now without
  changing that cart. Guest checkout remains available with Cash on Delivery;
  signed-in standard users can use the same checkout with a persistent cart.
  On sign-in, guest cart items merge into the account cart subject to current
  stock and quantity limits. Delivery fee is ₱0.00. Product prices and stock
  are revalidated on the server at checkout. Orders reserve stock immediately
  but do not count as sales until an administrator marks delivery complete and
  confirms cash collection.
  Expired or invalid sign-in cookies are cleared on public pages; they do not
  prevent visitors from browsing the catalog.
- Administrators can review all orders from **Orders**. Order status moves
  through Pending, Processing, Out for delivery, and Delivered; cancellation
  before delivery restores reserved stock once. A completed Sales record is
  linked to the order only after COD collection, so pending orders do not
  inflate sales, revenue, or profit reports.
- Catalog cards open a read-only detail page. It shows stored product
  identifiers and the product row's fixed size/color as plain text. Each
  product row represents its own stocked size/color option; the system does not
  model grouped variant choices, descriptions, size charts, or shipping/return
  policies that are not stored in the database.
- Access tokens expire after 15 minutes and refresh tokens rotate for up to
  seven days. Cookies are HttpOnly and same-site; state-changing cookie
  requests require CSRF tokens. The sign-in form validates required fields and
  email syntax on both client and server, and credential failures use the same
  generic message. Password recovery is not currently available because no
  email delivery service is configured; no recovery link is shown.
- Sign-in attempts are independently limited by IP and a keyed hash of the
  normalized account identifier. Configure `LOGIN_IP_RATE_LIMIT` and
  `LOGIN_IDENTIFIER_RATE_LIMIT` using Flask-Limiter rate-limit syntax.
- For production, set `APP_ENV=production`, distinct random `SECRET_KEY` and
  `JWT_SECRET_KEY` values, use HTTPS with `JWT_COOKIE_SECURE=true`, and configure
  a shared Redis backend via `RATELIMIT_STORAGE_URI` (use `rediss://` when
  supported by your deployment). Production startup rejects non-Redis or
  in-memory rate storage and insecure auth cookies. If the rate-limit backend
  becomes unavailable, requests fail closed with a generic service-unavailable
  response rather than bypassing the limits.
- Before enabling password recovery, configure a real email delivery provider
  and add a complete expiring, single-use token flow; do not send recovery
  tokens through logs or show them in the UI.

## Features

- Product catalog with photo uploads, unique SKUs and barcodes, generated Code 128 barcode labels,
  stock quantities, restocking, and configurable low-stock alerts.
- Unsold products with no order history can be permanently deleted; products
  with sales or order history are archived instead, preserving records, and
  can be restored later.
- Searchable point of sale with stock validation, automatic inventory deduction,
  and transaction history.
- Seven-day sales and expense charts, top-selling products, and product revenue
  reporting, estimated profit, and inventory value at cost.
- Expense tracking and printable invoices, with PDF downloads.
- Clothing sizes and colors, point-of-sale change calculation, and itemized
  receipts recording tendered cash and change.
- JWT-based account authentication, admin/user roles, configurable per-user
  permissions, account activation, and security audit history.
- Persistent per-user shopping carts, guest-cart merging at sign-in, COD
  checkout, and account-owned order history.

## Tests

Install the dependencies, then run:

```powershell
python -m pytest
```

On Windows, if Python is available only in the project virtual environment,
use `.\.venv\Scripts\python.exe -m flask --app app db upgrade` and
`.\.venv\Scripts\python.exe -m pytest`.
