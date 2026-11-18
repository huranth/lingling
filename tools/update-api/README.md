# update manifest

The client reads this on launch to check whether it is stale.

```text
GET https://lingling-update.vercel.app/api/latest
```

```json
{"schema":1,"package":"lingling","latest":"3.0.0",
 "min_supported":null,"notice":"Lingling uses Route64. After updating, run: lingling setup",
 "url":"https://pypi.org/project/lingling/","checked_at":1760000000}
```

One route, no state, no database, no auth. `latest` comes from PyPI on each
request, so a deploy cannot advertise a version nobody published. The client
falls back to PyPI when this endpoint is unreachable.

`notice` and `min_supported` are the two fields PyPI cannot carry. Both are
environment overrides; so are the package name, route, URLs, cache headers,
and retired versions:

```text
LINGLING_PACKAGE          package name
LINGLING_API_ROUTE        endpoint path
LINGLING_PYPI_URL         registry JSON URL
LINGLING_PROJECT_URL      project page
LINGLING_UPDATE_SCHEMA    manifest schema
LINGLING_PYPI_TIMEOUT     registry timeout
LINGLING_CDN_CACHE        CDN cache policy
LINGLING_BROWSER_CACHE    browser cache policy
LINGLING_NOTICE           release notice
LINGLING_MIN_SUPPORTED    oldest supported version
LINGLING_RETIRED          comma-separated retired versions
```

Deploy:

```bash
cd tools/update-api
vercel link --yes --project lingling-update
vercel --prod --yes
```

Check:

```bash
curl -sS https://lingling-update.vercel.app/api/latest
```

The response must have `schema: 1` and a `latest` equal to
`https://pypi.org/pypi/lingling/json`.
