vcl 4.0;

# Origin: the existing single-tier nginx that fronts app1-4
backend default {
    .host = "nginx-origin";
    .port = "80";
    .connect_timeout   = 5s;
    .first_byte_timeout = 30s;
    .between_bytes_timeout = 30s;
}

sub vcl_recv {
    # Strip cookies so they don't prevent caching
    unset req.http.Cookie;

    # Only cache GET requests on the redirect path /r/<token>
    if (req.method == "GET" && req.url ~ "^/r/") {
        return (hash);
    }

    # Everything else (POST /api/qr/create, analytics, etc.) bypasses cache
    return (pass);
}

sub vcl_backend_response {
    # Cache 302 redirect responses for /r/ paths
    if (bereq.url ~ "^/r/" && beresp.status == 302) {
        set beresp.ttl = 60s;
        unset beresp.http.Set-Cookie;
        return (deliver);
    }

    # Do not cache errors
    if (beresp.status >= 400) {
        set beresp.ttl = 0s;
        set beresp.uncacheable = true;
        return (deliver);
    }
}

sub vcl_deliver {
    # Add X-Cache header for observability
    if (obj.hits > 0) {
        set resp.http.X-Cache = "HIT";
        set resp.http.X-Cache-Hits = obj.hits;
    } else {
        set resp.http.X-Cache = "MISS";
    }
}
