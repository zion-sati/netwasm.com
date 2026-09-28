# netwasm.com

Source for the NetWasm product site at [www.netwasm.com](https://www.netwasm.com/).

The site is static and has no build step. Serve this directory with any local
HTTP server, for example:

```sh
python3 -m http.server 8080
```

GitHub Pages deploys the explicit public-file inventory through the reviewed
Actions workflow. The checked-in `CNAME` keeps the custom domain attached.
