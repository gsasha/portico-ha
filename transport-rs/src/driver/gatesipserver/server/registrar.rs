//! The SIP registrar: REGISTER handling, the nonce-host table, the registration expiry and the "can dial" bookkeeping (`server.go` `onRegister`, `noteRegistered`, `maybePublishCaps`).

use super::*;

/// The last good REGISTER: where the station can be reached (dial.go `registration`).
pub(super) struct Registration {
    /// the REGISTER's UDP source: where the outbound call of dial.go goes
    pub(super) addr: SocketAddr,
    pub(super) at: Instant,
}

/// The expiry a REGISTER asks for: the Contact header's `expires=` parameter when it has one,
/// else the `Expires` header; `None` when neither is a number. Only the parameters after the
/// URI count (`<sip:x@h;expires=0>` is a URI parameter, `;x-expires=0` another name, and a
/// quoted display name is not a parameter).
pub(super) fn register_expiry(req: &ParsedRequest) -> Option<i64> {
    let contact = req.header("Contact");
    if let Some(n) = contact_expires(contact) {
        return Some(n);
    }
    req.header("Expires").trim().parse().ok()
}

/// The `expires` parameter of a Contact value, or `None`.
pub(super) fn contact_expires(contact: &str) -> Option<i64> {
    // Skip a quoted display name, then the URI (inside `<...>` when there is one).
    let mut in_quote = false;
    let mut prev_escape = false;
    let mut rest = None;
    for (i, c) in contact.char_indices() {
        if in_quote {
            if prev_escape {
                prev_escape = false;
            } else if c == '\\' {
                prev_escape = true;
            } else if c == '"' {
                in_quote = false;
            }
        } else if c == '"' {
            in_quote = true;
        } else if c == '>' {
            rest = Some(&contact[i + 1..]);
            break;
        }
    }
    // Without `<`, the URI ends at the first `;` and the parameters follow it.
    let params = match rest {
        Some(r) => r,
        None if contact.contains('<') => return None,
        None => contact.split_once(';').map_or("", |(_, p)| p),
    };
    for p in params.split(';') {
        if let Some((k, v)) = p.split_once('=') {
            if k.trim().eq_ignore_ascii_case("expires") {
                return v
                    .trim()
                    .chars()
                    .take_while(char::is_ascii_digit)
                    .collect::<String>()
                    .parse()
                    .ok();
            }
        }
    }
    None
}

impl Server {
    pub(super) fn on_register(&self, req: &ParsedRequest, h: &Hdrs, addr: SocketAddr) {
        // the header as received, bytes and all: a username or uri that is not UTF-8 is hashed
        // as Go hashes it (R1-5)
        let auth = req.header_raw("Authorization");
        let challenge = || {
            // the store remembers which host the nonce went to (not in Go, difference 50)
            let nonce = self.nonces.issue_for(addr.ip().to_canonical());
            self.reply_h(
                h,
                addr,
                401,
                "Unauthorized",
                "",
                &[format!(
                    "WWW-Authenticate: Digest realm=\"{}\", nonce=\"{}\", algorithm=MD5",
                    self.cfg.realm, nonce
                )],
                "",
            );
        };
        if auth.is_empty() {
            challenge();
            return;
        }
        let res = verify_digest_authorization_raw(
            &self.nonces,
            auth,
            "REGISTER",
            &self.cfg.realm,
            &self.cfg.password,
        );
        if res == DigestResult::Stale {
            // GO-16: a nonce this server never issued, or one past its TTL (a replayed header,
            // or a station reusing an old nonce): re-challenge instead of accepting. A
            // legitimate station answers the fresh 401.
            self.logf(format!(
                "gate_sip_server: REGISTER from {addr} with an unknown or expired nonce, re-challenging"
            ));
            challenge();
            return;
        }
        // DELIBERATE DIFFERENCE from Go (difference 50): a nonce is good only for the host it was
        // issued to. Go accepts a captured Authorization from ANY allowed host while the nonce
        // lives (5 minutes: the digest binds neither the URI nor an `nc`), and `reg.addr` is now
        // where the outbound INVITE of dial.go goes, so a replay from another host would redirect
        // the "talk to the gate" call. Such a REGISTER is re-challenged (a fresh nonce for ITS
        // address), exactly like an unknown nonce; the registration is not touched. A password
        // that does not match is still judged first (403), and a nonce reused by the host it was
        // issued to behaves as in Go (200).
        if res == DigestResult::Ok {
            let nonce = String::from_utf8_lossy(&authorization_nonce_raw(auth)).into_owned();
            let bound = self.nonces.issued_to(&nonce);
            if bound != Some(addr.ip().to_canonical()) {
                self.logf(format!(
                    "gate_sip_server: REGISTER from {addr} with a nonce issued to {}, re-challenging",
                    bound.map_or("no host".to_string(), |ip| ip.to_string())
                ));
                challenge();
                return;
            }
        }
        if res != DigestResult::Ok {
            // DELIBERATE DIFFERENCE from gate-sip (the owner's decision 2026-10-21, DEV-518: refuse
            // bad digests). gate-sip accepts a mismatching digest and only logs it; here the
            // REGISTER is refused with 403, `Registered` is NOT set, and the mismatch is counted
            // and logged. 403 and not a second 401: re-challenging a station that holds the wrong
            // secret would loop.
            lock(&self.state).st.digest_mismatch += 1;
            self.logf(format!(
                "gate_sip_server: REGISTER from {addr} digest=MISMATCH — refused 403"
            ));
            self.reply_h(h, addr, 403, "Forbidden", "", &[], "");
            return;
        }
        // DELIBERATE DIFFERENCE from Go (server.go onRegister, 447 to 453: Expires is never
        // read, so a de-registration registers and is answered `Expires: 300`): a REGISTER whose
        // expiry is 0 (`Expires: 0`, or `expires=0` on the Contact, which wins) clears the
        // registration, CanDial goes false and is republished (RFC 3261 §10.2.2). It is answered
        // 200 with `Expires: 0`; every other REGISTER keeps Go's wire shape.
        let deregister = register_expiry(req) == Some(0);
        {
            let mut st = lock(&self.state);
            st.st.digest_ok += 1;
            if deregister {
                st.st.registered = false;
                st.st.registered_from = String::new();
                st.reg = None;
                if let Some(t) = st.reg_timer.take() {
                    t.cancel();
                }
                self.maybe_publish_caps_locked(&mut st);
            } else {
                st.st.registered = true;
                st.st.registered_from = addr.to_string();
                self.note_registered_locked(&mut st, addr);
            }
        }
        if deregister {
            self.logf(format!(
                "gate_sip_server: REGISTER from {addr} digest=OK, expires=0: registration removed"
            ));
            self.reply_h(h, addr, 200, "OK", "", &["Expires: 0".to_string()], "");
            return;
        }
        self.logf(format!("gate_sip_server: REGISTER from {addr} digest=OK"));
        self.reply_h(h, addr, 200, "OK", "", &["Expires: 300".to_string()], "");
    }

    /// `noteRegisteredLocked`: records a good REGISTER and republishes CanDial if it changed.
    pub(super) fn note_registered_locked(&self, st: &mut State, addr: SocketAddr) {
        st.reg = Some(Registration {
            addr,
            at: Instant::now(),
        });
        st.reg_unix = unix_now();
        if st.closed {
            return;
        }
        // The expiry timer runs with dial off too: the station-registered signal (DEV-518) must
        // fall back to false by itself.
        if let Some(t) = st.reg_timer.take() {
            t.cancel();
        }
        let token = CancelToken::new();
        st.reg_timer = Some(token.clone());
        self.spawn_timer(st.reg_ttl + Duration::from_millis(20), token, |s| {
            let mut st = lock(&s.state);
            s.maybe_publish_caps_locked(&mut st);
        });
        self.maybe_publish_caps_locked(st);
    }

    pub(super) fn registered_locked(st: &State) -> bool {
        st.reg
            .as_ref()
            .is_some_and(|r| r.at.elapsed() <= st.reg_ttl)
    }

    pub(super) fn can_dial_locked(&self, st: &State) -> bool {
        self.cfg.dial_enabled && !st.closed && Self::registered_locked(st)
    }

    /// The UDP source of the last good REGISTER (dial.go `s.reg.addr`).
    pub fn registration_addr(&self) -> Option<SocketAddr> {
        lock(&self.state).reg.as_ref().map(|r| r.addr)
    }

    /// `canDial`: `dial_enabled` AND the station registered recently enough to be called.
    pub fn can_dial(&self) -> bool {
        let st = lock(&self.state);
        self.can_dial_locked(&st)
    }

    /// DEV-518: (registered now, unix seconds of the last good REGISTER). Independent of
    /// `dial_enabled`.
    pub fn station(&self) -> (bool, u64) {
        let st = lock(&self.state);
        (Self::registered_locked(&st), st.reg_unix)
    }

    /// `maybePublishCapsLocked`: queues a capabilities republish when what the last one said
    /// (CanDial, StationRegistered, LastRegisterAt) differs from now. The baseline is
    /// (false, false, 0), the configure reply of a server that has not heard a REGISTER yet.
    /// With dial_enabled off CanDial stays false, but the registration fields still move
    /// (DEV-518), so the event stream carries one republish per REGISTER and one at expiry.
    pub(super) fn maybe_publish_caps_locked(&self, st: &mut State) {
        if st.closed {
            return;
        }
        let now = (
            self.can_dial_locked(st),
            Self::registered_locked(st),
            st.reg_unix,
        );
        if now == st.caps_pub {
            return;
        }
        st.caps_pub = now;
        self.emit(Event::Caps {
            device_id: self.cfg.device_id.clone(),
            caps: GateSipServer::capabilities_for_station(&self.cfg, now.0, now.1, now.2),
        });
    }
}

/// Wall-clock unix seconds (0 if the clock is before the epoch).
fn unix_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}
