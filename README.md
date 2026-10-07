# mcp-copernicus

MCP server for the **Copernicus Data Space Ecosystem** — catalogue search,
object-storage download and openEO processing, ready to use with
[OpenCode](https://opencode.ai) or any other MCP client.

```
copernicus_status            endpoint/token health check
copernicus_search_products   OData search (collection, dates, bbox, cloud cover)
copernicus_get_product       full metadata for one product
copernicus_download_product  download an archive to disk
copernicus_list_product_files  browse inside a SAFE package
copernicus_product_s3_path   product → object-store key
copernicus_s3_list           list the eodata bucket
copernicus_s3_head           object metadata
copernicus_s3_download       download one object
openeo_collections           available collections
openeo_describe_collection   bands, extents, parameters
openeo_execute_graph         synchronous processing
openeo_start_job             asynchronous batch job
openeo_jobs                  list jobs
openeo_job_info              status / assets / errors
openeo_job_logs              job logs
openeo_download_job_result   fetch job outputs
openeo_ndvi_job              NDVI batch job in one call
openeo_ndvi_graph            build the NDVI graph without running it
```

## Installation

```bash
git clone https://github.com/rsdenck/mcp-copernicus.git
cd mcp-copernicus
uv venv --python 3.11 .venv
uv pip install -e .
```

## Credentials

Register at <https://dataspace.copernicus.eu/> and create an OAuth client in
the account settings. Then export:

```bash
export CDSE_CLIENT_ID='sh-...'
export CDSE_CLIENT_SECRET='...'
```

Alternatively use your CDSE login (password grant):

```bash
export CDSE_USERNAME='you@example.com'
export CDSE_PASSWORD='...'
```

Optional overrides:

| Variable | Default |
| --- | --- |
| `CDSE_ODATA_URL` | `https://catalogue.dataspace.copernicus.eu/odata/v1` |
| `CDSE_DOWNLOAD_URL` | `https://download.dataspace.copernicus.eu/odata/v1` |
| `CDSE_OPENEO_URL` | `https://openeo.dataspace.copernicus.eu` |
| `CDSE_S3_ENDPOINT` | `https://eodata.dataspace.copernicus.eu` |
| `CDSE_S3_BUCKET` | `eodata` |
| `CDSE_S3_REGION` | `default` |
| `CDSE_S3_ACCESS_KEY_ID` / `CDSE_S3_SECRET_ACCESS_KEY` | *(none)* |
| `CDSE_DOWNLOAD_DIR` | `~/copernicus-data` |
| `CDSE_TIMEOUT` | `120` |

## Running standalone

```bash
.venv/bin/mcp-copernicus        # stdio
```

## Registering with OpenCode

```jsonc
// ~/.config/opencode/opencode.json
{
  "mcp": {
    "servers": {
      "copernicus": {
        "type": "local",
        "command": ["/root/mcps/mcp-copernicus/.venv/bin/mcp-copernicus"],
        "environment": {
          "CDSE_CLIENT_ID": "...",
          "CDSE_CLIENT_SECRET": "..."
        },
        "enabled": true
      }
    }
  }
}
```

## Known limitation: object storage

The temporary keys returned by `s3-keys-manager.cloudferro.com` for an OAuth
**service-account** client are rejected by the `eodata` object store with
`SignatureDoesNotMatch`. This was verified with four independent SigV4
implementations (botocore, s3cmd, `curl --aws-sigv4` and a hand-rolled
signer), on both the CloudFerro and OTC endpoints, and up to 12 minutes after
key creation, so it is not a propagation delay.

The catalogue download path
(`Products({id})/Value` → `download.dataspace.copernicus.eu`) works normally
and is the recommended way to fetch products.

If you have keys generated in the CDSE web UI
(<https://eodata-s3keysmanager.dataspace.copernicus.eu/>), set them through
`CDSE_S3_ACCESS_KEY_ID` / `CDSE_S3_SECRET_ACCESS_KEY` and the S3 tools will
use them instead. Otherwise `copernicus_status` reports the exact server
error rather than hiding it.

## Licence

MIT
