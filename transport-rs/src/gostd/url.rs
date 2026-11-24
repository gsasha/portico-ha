//! `net/url`'s `Parse` and the pieces of `URL` the observer's `Configure` reads, with Go's
//! error texts: the observer reports `gate_sip_url: <err>` to the operator, so the wording is
//! part of the behaviour. Checked against the real Go by `tests/golden/obs_config.json`.

use crate::gostd::strconv::go_quote;

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Url {
    pub scheme: String,
    pub opaque: String,
    pub has_user: bool,
    /// the host as Go stores it: percent escapes decoded
    pub host: String,
    pub path: String,
    pub raw_path: String,
    pub force_query: bool,
    pub raw_query: String,
    pub fragment: String,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Mode {
    Path,
    Host,
    Zone,
    UserPassword,
    Fragment,
}

fn should_escape(c: u8, mode: Mode) -> bool {
    if c.is_ascii_alphanumeric() {
        return false;
    }
    if matches!(mode, Mode::Host | Mode::Zone)
        && matches!(
            c,
            b'!' | b'$'
                | b'&'
                | b'\''
                | b'('
                | b')'
                | b'*'
                | b'+'
                | b','
                | b';'
                | b'='
                | b':'
                | b'['
                | b']'
                | b'<'
                | b'>'
                | b'"'
        )
    {
        return false;
    }
    match c {
        b'-' | b'_' | b'.' | b'~' => return false,
        b'$' | b'&' | b'+' | b',' | b'/' | b':' | b';' | b'=' | b'?' | b'@' => match mode {
            Mode::Path => return c == b'?',
            Mode::UserPassword => return matches!(c, b'@' | b'/' | b'?' | b':'),
            Mode::Fragment => return false,
            _ => {}
        },
        _ => {}
    }
    if mode == Mode::Fragment && matches!(c, b'!' | b'(' | b')' | b'*') {
        return false;
    }
    true
}

/// `validEncoded`.
fn valid_encoded(s: &str, mode: Mode) -> bool {
    s.bytes().all(|c| match c {
        b'!' | b'$' | b'&' | b'\'' | b'(' | b')' | b'*' | b'+' | b',' | b';' | b'=' | b':'
        | b'@' | b'[' | b']' | b'%' => true,
        _ => !should_escape(c, mode),
    })
}

fn unhex(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    }
}

/// `url.unescape`; the result is bytes (a decoded escape may be invalid UTF-8).
fn unescape(s: &str, mode: Mode) -> Result<Vec<u8>, String> {
    let b = s.as_bytes();
    // count %, check for invalid
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'%' => {
                if i + 2 >= b.len() || unhex(b[i + 1]).is_none() || unhex(b[i + 2]).is_none() {
                    let end = b.len().min(i + 3);
                    let bad = String::from_utf8_lossy(&b[i..end]).into_owned();
                    return Err(format!("invalid URL escape {}", go_quote(&bad)));
                }
                let (h, l) = (unhex(b[i + 1]), unhex(b[i + 2]));
                // The RFC allows %-encoding in a host only for IPv6 zone identifiers.
                if mode == Mode::Host && h.unwrap() < 8 && &b[i..i + 3] != b"%25" {
                    let bad = String::from_utf8_lossy(&b[i..i + 3]).into_owned();
                    return Err(format!("invalid URL escape {}", go_quote(&bad)));
                }
                if mode == Mode::Zone {
                    let v = h.unwrap() << 4 | l.unwrap();
                    if &b[i..i + 3] != b"%25" && v != b' ' && should_escape(v, Mode::Host) {
                        let bad = String::from_utf8_lossy(&b[i..i + 3]).into_owned();
                        return Err(format!("invalid URL escape {}", go_quote(&bad)));
                    }
                }
                i += 3;
            }
            c => {
                if matches!(mode, Mode::Host | Mode::Zone) && c < 0x80 && should_escape(c, mode) {
                    return Err(format!(
                        "invalid character {} in host name",
                        go_quote(&(c as char).to_string())
                    ));
                }
                i += 1;
            }
        }
    }
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            out.push(unhex(b[i + 1]).unwrap() << 4 | unhex(b[i + 2]).unwrap());
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    Ok(out)
}

fn lossy(b: Vec<u8>) -> String {
    String::from_utf8(b).unwrap_or_else(|e| String::from_utf8_lossy(e.as_bytes()).into_owned())
}

/// `url.escape(s, mode)`.
fn escape(s: &str, mode: Mode) -> String {
    let mut out = String::new();
    for &c in s.as_bytes() {
        if should_escape(c, mode) {
            out.push_str(&format!("%{c:02X}"));
        } else {
            out.push(c as char);
        }
    }
    out
}

/// `escape(host, encodeHost)`: the host as `URL.String` writes it.
pub fn escape_host(host: &str) -> String {
    escape(host, Mode::Host)
}

fn valid_optional_port(port: &str) -> bool {
    if port.is_empty() {
        return true;
    }
    let Some(rest) = port.strip_prefix(':') else {
        return false;
    };
    rest.bytes().all(|c| c.is_ascii_digit())
}

fn parse_host(host: &str) -> Result<String, String> {
    if host.starts_with('[') {
        let Some(i) = host.rfind(']') else {
            return Err("missing ']' in host".to_string());
        };
        let colon_port = &host[i + 1..];
        if !valid_optional_port(colon_port) {
            return Err(format!("invalid port {} after host", go_quote(colon_port)));
        }
        if let Some(zone) = host[..i].find("%25") {
            let h1 = unescape(&host[..zone], Mode::Host)?;
            let h2 = unescape(&host[zone..i], Mode::Zone)?;
            let h3 = unescape(&host[i..], Mode::Host)?;
            let mut all = h1;
            all.extend(h2);
            all.extend(h3);
            return Ok(lossy(all));
        }
    } else if let Some(i) = host.rfind(':') {
        let colon_port = &host[i..];
        if !valid_optional_port(colon_port) {
            return Err(format!("invalid port {} after host", go_quote(colon_port)));
        }
    }
    Ok(lossy(unescape(host, Mode::Host)?))
}

fn valid_userinfo(s: &str) -> bool {
    s.bytes().all(|r| {
        r.is_ascii_alphanumeric()
            || matches!(
                r,
                b'-' | b'.'
                    | b'_'
                    | b':'
                    | b'~'
                    | b'!'
                    | b'$'
                    | b'&'
                    | b'\''
                    | b'('
                    | b')'
                    | b'*'
                    | b'+'
                    | b','
                    | b';'
                    | b'='
                    | b'%'
                    | b'@'
            )
    })
}

fn parse_authority(authority: &str) -> Result<(bool, String), String> {
    match authority.rfind('@') {
        None => Ok((false, parse_host(authority)?)),
        Some(i) => {
            let host = parse_host(&authority[i + 1..])?;
            let userinfo = &authority[..i];
            if !valid_userinfo(userinfo) {
                return Err("net/url: invalid userinfo".to_string());
            }
            match userinfo.split_once(':') {
                None => {
                    unescape(userinfo, Mode::UserPassword)?;
                }
                Some((u, p)) => {
                    unescape(u, Mode::UserPassword)?;
                    unescape(p, Mode::UserPassword)?;
                }
            }
            Ok((true, host))
        }
    }
}

/// `getScheme`: (scheme, rest), or the "missing protocol scheme" error.
fn get_scheme(raw: &str) -> Result<(String, String), String> {
    let b = raw.as_bytes();
    for (i, &c) in b.iter().enumerate() {
        match c {
            b'a'..=b'z' | b'A'..=b'Z' => {}
            b'0'..=b'9' | b'+' | b'-' | b'.' => {
                if i == 0 {
                    return Ok((String::new(), raw.to_string()));
                }
            }
            b':' => {
                if i == 0 {
                    return Err("missing protocol scheme".to_string());
                }
                return Ok((raw[..i].to_string(), raw[i + 1..].to_string()));
            }
            _ => return Ok((String::new(), raw.to_string())),
        }
    }
    Ok((String::new(), raw.to_string()))
}

fn parse1(raw: &str) -> Result<Url, String> {
    if raw.bytes().any(|c| c < 0x20 || c == 0x7f) {
        return Err("net/url: invalid control character in URL".to_string());
    }
    let mut url = Url::default();
    let (scheme, mut rest) = get_scheme(raw)?;
    url.scheme = scheme.to_ascii_lowercase();
    if rest.ends_with('?') && rest.matches('?').count() == 1 {
        url.force_query = true;
        rest.pop();
    } else {
        if let Some((a, q)) = rest.split_once('?') {
            url.raw_query = q.to_string();
            rest = a.to_string();
        }
    }
    if !rest.starts_with('/') {
        if !url.scheme.is_empty() {
            url.opaque = rest;
            return Ok(url);
        }
        let segment = rest.split('/').next().unwrap_or("");
        if segment.contains(':') {
            return Err("first path segment in URL cannot contain colon".to_string());
        }
    }
    if (!url.scheme.is_empty() || !rest.starts_with("///")) && rest.starts_with("//") {
        let after = &rest[2..];
        let (authority, tail) = match after.find('/') {
            Some(i) => (&after[..i], after[i..].to_string()),
            None => (after, String::new()),
        };
        let (user, host) = parse_authority(authority)?;
        url.has_user = user;
        url.host = host;
        rest = tail;
    }
    // setPath
    let path = lossy(unescape(&rest, Mode::Path)?);
    url.raw_path = if escape(&path, Mode::Path) == rest {
        String::new()
    } else {
        rest.clone()
    };
    url.path = path;
    Ok(url)
}

/// `url.Parse`: the error is `parse "<url>": <cause>` exactly as `*url.Error` prints it.
pub fn parse(raw: &str) -> Result<Url, String> {
    let (u, frag) = match raw.split_once('#') {
        Some((a, f)) => (a, f),
        None => (raw, ""),
    };
    let mut url = parse1(u).map_err(|e| format!("parse {}: {e}", go_quote(u)))?;
    if !frag.is_empty() {
        // setFragment
        let f =
            unescape(frag, Mode::Fragment).map_err(|e| format!("parse {}: {e}", go_quote(raw)))?;
        url.fragment = lossy(f);
    }
    Ok(url)
}

impl Url {
    /// `URL.RequestURI()` of a URL without an opaque part: the escaped path (`/` when empty),
    /// then `?` and the raw query (slice 7b: the guard's request target).
    pub fn request_uri(&self) -> String {
        let mut r = self.escaped_path();
        if r.is_empty() {
            r.push('/');
        }
        if self.force_query || !self.raw_query.is_empty() {
            r.push('?');
            r.push_str(&self.raw_query);
        }
        r
    }

    /// `URL.EscapedPath()`: `raw_path` when it is a valid encoding of `path`, else `path`
    /// escaped.
    pub fn escaped_path(&self) -> String {
        if !self.raw_path.is_empty() && valid_encoded(&self.raw_path, Mode::Path) {
            if let Ok(p) = unescape(&self.raw_path, Mode::Path) {
                if p == self.path.as_bytes() {
                    return self.raw_path.clone();
                }
            }
        }
        if self.path == "*" {
            return "*".to_string();
        }
        escape(&self.path, Mode::Path)
    }

    /// `URL.Hostname()`.
    pub fn hostname(&self) -> String {
        split_host_port(&self.host).0
    }

    /// `URL.Port()`: empty when the host has none.
    pub fn port(&self) -> String {
        split_host_port(&self.host).1
    }

    /// `(Hostname(), Port())` of the URL as `http.NewRequest` leaves it: an empty port is removed
    /// from the host first (`removeEmptyPort`), then the host is split at its last colon.
    pub fn request_host_port(&self) -> (String, String) {
        split_host_port(&remove_empty_port(&self.host))
    }

    /// What `net/http` dials for this URL (`canonicalAddr`): `Hostname()` and `Port()`, the
    /// scheme's default port (`default_port`) when there is none.
    pub fn dial_addr(&self, default_port: &str) -> String {
        let (host, port) = self.request_host_port();
        crate::gostd::net::join_host_port(
            &host,
            if port.is_empty() {
                default_port
            } else {
                port.as_str()
            },
        )
    }

    /// The `Host` header `net/http` sends: `NewRequest` removes an empty port (`removeEmptyPort`).
    pub fn host_header(&self) -> String {
        remove_empty_port(&self.host)
    }
}

/// `removeEmptyPort`: `host:` is `host`, `[::1]:` is `[::1]`; other hosts are unchanged.
pub fn remove_empty_port(host: &str) -> String {
    let has_port = match (host.rfind(':'), host.rfind(']')) {
        (Some(c), Some(b)) => c > b,
        (Some(_), None) => true,
        _ => false,
    };
    if has_port {
        host.strip_suffix(':').unwrap_or(host).to_string()
    } else {
        host.to_string()
    }
}

fn split_host_port(host_port: &str) -> (String, String) {
    let mut host = host_port.to_string();
    let mut port = String::new();
    if let Some(colon) = host.rfind(':') {
        if valid_optional_port(&host[colon..]) {
            port = host[colon + 1..].to_string();
            host.truncate(colon);
        }
    }
    if host.starts_with('[') && host.ends_with(']') && host.len() >= 2 {
        host = host[1..host.len() - 1].to_string();
    }
    (host, port)
}

#[cfg(test)]
mod tests {
    use super::*;

    // R2-7: what `net/http` dials and sends for the host forms a console can be configured with;
    // every row run through the real Go (`url.Parse` + `Hostname()` / `Port()` + `JoinHostPort`,
    // `NewRequest`'s `removeEmptyPort`) in the Go 1.23 image.
    #[test]
    fn the_dial_target_and_host_header_are_net_https() {
        for (host, dial, header) in [
            ("host", "host:443", "host"),
            ("host:7443", "host:7443", "host:7443"),
            ("[::1]:7443", "[::1]:7443", "[::1]:7443"),
            ("[::1]", "[::1]:443", "[::1]"),
            ("host:", "host:443", "host"),
            ("[::1]:", "[::1]:443", "[::1]"),
            // bare IPv6 literals: Go cuts at the last colon, so these dial a nonsense host
            ("::1", "[:]:1", "::1"),
            ("fe80::1", "[fe80:]:1", "fe80::1"),
            ("2001:db8::", "[2001:db8]:443", "2001:db8:"),
            ("fe80::", "fe80:443", "fe80:"),
        ] {
            let u = parse(&format!("https://{host}/proxy")).unwrap();
            assert_eq!(u.dial_addr("443"), dial, "{host}");
            assert_eq!(u.host_header(), header, "{host}");
        }
        assert_eq!(
            parse("https://host:abc/proxy").unwrap_err(),
            "parse \"https://host:abc/proxy\": invalid port \":abc\" after host"
        );
        assert_eq!(parse("http://h:/x").unwrap().dial_addr("80"), "h:80");
    }

    #[test]
    fn parse_matches_the_cases_run_through_go() {
        for (raw, err) in [
            (
                "http://h:8091/%zz",
                "parse \"http://h:8091/%zz\": invalid URL escape \"%zz\"",
            ),
            ("http://[::1", "parse \"http://[::1\": missing ']' in host"),
            (
                "http://h:x",
                "parse \"http://h:x\": invalid port \":x\" after host",
            ),
            (
                "http://h /",
                "parse \"http://h /\": invalid character \" \" in host name",
            ),
            (
                "http://ex%41mple.com",
                "parse \"http://ex%41mple.com\": invalid URL escape \"%41\"",
            ),
            (
                "http://h\n",
                "parse \"http://h\\n\": net/url: invalid control character in URL",
            ),
            ("://x", "parse \"://x\": missing protocol scheme"),
            (
                "1http://h",
                "parse \"1http://h\": first path segment in URL cannot contain colon",
            ),
            (
                "http://h/%",
                "parse \"http://h/%\": invalid URL escape \"%\"",
            ),
            (
                "http://h/#%zz",
                "parse \"http://h/#%zz\": invalid URL escape \"%zz\"",
            ),
            (
                "http://us er@h",
                "parse \"http://us er@h\": net/url: invalid userinfo",
            ),
            (
                "http://[::1]x",
                "parse \"http://[::1]x\": invalid port \"x\" after host",
            ),
        ] {
            assert_eq!(parse(raw).unwrap_err(), err, "{raw:?}");
        }
        let u = parse("http://h/%2F").unwrap();
        assert_eq!((u.path.as_str(), u.raw_path.as_str()), ("//", "/%2F"));
        let u = parse("http:/h").unwrap();
        assert_eq!((u.host.as_str(), u.path.as_str()), ("", "/h"));
        let u = parse("http://[fe80::1%25en0]:80").unwrap();
        assert_eq!(
            (u.host.as_str(), u.hostname().as_str()),
            ("[fe80::1%en0]:80", "fe80::1%en0")
        );
        assert!(parse("http://a@b@c").unwrap().has_user);
        let u = parse("h:80").unwrap();
        assert_eq!((u.scheme.as_str(), u.opaque.as_str()), ("h", "80"));
        assert_eq!(parse("HTTP://H").unwrap().scheme, "http");
        assert!(parse("http://h:8091?").unwrap().force_query);
    }
}
