//! The device handlers: `configure` ... `hangup` (`configureHandler`, `dialHandler`, `hangupHandler`, ...).

use super::*;

impl Hub {
    /// `lookupDeviceIn`: parses a device-addressed request and finds its driver.
    pub(super) fn lookup_device(
        &self,
        params: &Params,
    ) -> Result<(Arc<dyn Driver>, DeviceParams), IpcError> {
        let p = DeviceParams::decode(params)?;
        let id = p.key();
        match self.get(&id) {
            Some(d) => Ok((d, p)),
            None => Err(IpcError::new("unknown_device", id)),
        }
    }

    /// `configureHandler`: a device_id already configured is refused with `already_configured`
    /// before anything is started (a second driver for the same device would register the same
    /// SIP identity twice); Python deconfigures first on a reload.
    pub(super) async fn configure(&self, params: Params) -> HandlerResult {
        let shutting_down = || IpcError::new("shutting_down", "transport is stopping");
        if self.ctx.is_cancelled() {
            return Err(shutting_down());
        }
        let v = gojson::decode(raw_str(&params), "configureParams", &CONFIGURE_PARAMS)
            .map_err(bad_params)?;
        let key = gojson::str_at(&v, 0);
        let config = match v.get(1) {
            Some(Some(Value::Object(m))) => m.clone(),
            _ => serde_json::Map::new(), // Go's nil map
        };
        register_config_secrets(&config);
        #[cfg(feature = "test-seams")]
        let made = lock(&self.test_drivers).get(key.as_str()).cloned();
        #[cfg(not(feature = "test-seams"))]
        let made: Option<std::sync::Arc<dyn Fn() -> Box<dyn Driver> + Send + Sync>> = None;
        let new_driver = match made {
            Some(m) => m,
            None => match crate::driver::registry().get(key.as_str()).copied() {
                Some(ctor) => std::sync::Arc::new(ctor) as _,
                None => return Err(IpcError::new("unknown_driver", key)),
            },
        };
        // `Configure` may resolve a host name (blocking): keep it off the runtime thread.
        let cfg = config.clone();
        let configured = tokio::task::spawn_blocking(move || {
            let mut d = (*new_driver)();
            d.configure(&cfg).map(|()| d)
        })
        .await;
        let d: Arc<dyn Driver> = match configured {
            Ok(Ok(d)) => Arc::from(d),
            Ok(Err(e)) => return Err(IpcError::new("configure_failed", e.message())),
            Err(_) => return Err(IpcError::new("configure_failed", "internal error")),
        };
        let id = registry_id(&*d, &config);
        if self.get(&id).is_some() {
            return Err(IpcError::new("already_configured", id));
        }
        // Start actually touches the network (a bind, or a real REGISTER): the activation gate
        // of the design doc (§7) applies from here, not from Configure alone.
        // `relayClosingSink{...}.ForDevice(id)`: the per-device sink, inside the relay-closing one
        let sink: Arc<dyn EventSink> = Arc::new(RelayClosingSink::new(
            Arc::new(DeviceSink {
                s: self.events.clone(),
                device_id: id.clone(),
            }),
            self.relays.clone(),
        ));
        if let Err(e) = d.start(&self.ctx, sink).await {
            return Err(IpcError::new("start_failed", e.message()));
        }
        // No drop guard around a started driver (R1-51): the future of a handler is owned by a
        // detached task (`Server::run`) that is never dropped before it finishes, so nothing here
        // can be cancelled between `start` and the registry.
        if !self.put_if_absent(&id, d.clone()) {
            // Lost a race with a concurrent configure of the same id.
            let _ = d.stop().await;
            return Err(IpcError::new("already_configured", id));
        }
        if self.ctx.is_cancelled() {
            // Shutdown drained the registry while Start ran; this driver would be left running
            // with nobody to stop it.
            self.remove(&id);
            let _ = d.stop().await;
            return Err(shutting_down());
        }
        d.capabilities_ready().await;
        Ok(Reply::Raw(d.capabilities().to_wire()))
    }

    /// `deconfigureHandler`: stops and forgets one driver without ending the process.
    pub(super) async fn deconfigure(&self, params: Params) -> HandlerResult {
        let p = DeviceParams::decode(&params)?;
        let id = p.key();
        let Some(d) = self.remove(&id) else {
            return Err(IpcError::new("unknown_device", id));
        };
        self.events.forget_device(&id);
        if let Err(e) = d.stop().await {
            return Err(IpcError::new("deconfigure_failed", e.message()));
        }
        Ok(json!({"ok": true}).into())
    }

    /// `dialHandler`: a driver that implements `CallDialer` gets a call id minted here,
    /// registered with the sink before the driver runs, and the reply carries it; any other
    /// driver takes the legacy path and the sink gives the device's next unannounced call to it.
    pub(super) async fn dial(&self, params: Params) -> HandlerResult {
        let (d, p) = self.lookup_device(&params)?;
        let id = p.key();
        if let Some(cd) = d.as_call_dialer() {
            let call_id = self.events.new_outbound_call(&id);
            if let Err(e) = cd.dial_call(&self.ctx, &call_id, p.target()).await {
                self.events.forget_call(&call_id);
                return Err(driver_error("dial_failed", &e));
            }
            return Ok(json!({"ok": true, "call_id": call_id}).into());
        }
        self.events.expect_dial(&id);
        if let Err(e) = d.dial(&self.ctx, p.target()).await {
            self.events.cancel_dial(&id);
            return Err(driver_error("dial_failed", &e));
        }
        Ok(json!({"ok": true}).into())
    }

    pub(super) async fn answer(&self, params: Params) -> HandlerResult {
        let started = std::time::Instant::now();
        let (d, p) = self.lookup_device(&params).inspect_err(|e| {
            crate::panic_hook::log_line(&format!(
                "portico-transport: answer request refused: {}",
                e.code
            ));
        })?;
        // the target is the phone's host and port: never logged
        crate::panic_hook::log_line(&format!(
            "portico-transport: answer request received for device {}",
            p.key()
        ));
        d.answer(&self.ctx, p.target())
            .await
            .map_err(|e| {
                crate::panic_hook::log_line(&format!(
                    "portico-transport: answer request for device {} refused after {} ms: {}",
                    p.key(),
                    started.elapsed().as_millis(),
                    e.message()
                ));
                driver_error("answer_failed", &e)
            })?;
        crate::panic_hook::log_line(&format!(
            "portico-transport: answer request for device {} granted after {} ms",
            p.key(),
            started.elapsed().as_millis()
        ));
        Ok(json!({"ok": true}).into())
    }

    pub(super) async fn open_relay(&self, params: Params) -> HandlerResult {
        let (d, _) = self.lookup_device(&params)?;
        d.open_relay(&self.ctx)
            .await
            .map_err(|e| driver_error("open_relay_failed", &e))?;
        Ok(json!({"ok": true}).into())
    }

    /// `externalRingHandler`.
    pub(super) async fn external_ring(&self, params: Params) -> HandlerResult {
        let v = gojson::decode(
            raw_str(&params),
            "externalRingParams",
            &EXTERNAL_RING_PARAMS,
        )
        .map_err(bad_params)?;
        let (device_id, call_id, hold) = (
            gojson::str_at(&v, 0),
            gojson::str_at(&v, 1),
            gojson::f64_at(&v, 2),
        );
        if device_id.is_empty() || call_id.is_empty() || !(0.0..=3600.0).contains(&hold) {
            return Err(bad_params(
                "external_ring needs device_id, call_id and a hold of 0..3600 seconds",
            ));
        }
        let Some(d) = self.get(&device_id) else {
            return Err(IpcError::new("unknown_device", device_id));
        };
        let Some(er) = d.as_external_ringer() else {
            return Err(IpcError::new(
                "not_supported",
                format!("{device_id} does not take external rings"),
            ));
        };
        self.events.adopt(&call_id, &device_id);
        if let Err(e) = er.external_ring(&call_id, Duration::from_secs_f64(hold)) {
            self.events.forget_call(&call_id);
            return Err(if e.is(Sentinel::Busy) {
                IpcError::new("busy", e.message())
            } else if e.is(Sentinel::NotSupported) {
                IpcError::new("not_supported", e.message())
            } else {
                IpcError::new("external_ring_failed", e.message())
            });
        }
        Ok(json!({"ok": true}).into())
    }

    /// `talkHealthHandler`.
    pub(super) async fn talk_health(&self, params: Params) -> HandlerResult {
        let id = gojson::decode(raw_str(&params), "", &TALK_HEALTH_PARAMS)
            .ok()
            .map(|v| gojson::str_at(&v, 0))
            .filter(|id| !id.is_empty())
            .ok_or_else(|| bad_params("talk_health needs a device_id"))?;
        let Some(d) = self.get(&id) else {
            return Err(IpcError::new("unknown_device", id));
        };
        let Some(h) = d.as_talk_healther() else {
            return Err(IpcError::new(
                "not_supported",
                format!("{id} has no talk health block"),
            ));
        };
        Ok(Reply::Raw(h.health().to_wire()))
    }

    /// `streamURLHandler`.
    pub(super) async fn stream_url(&self, params: Params) -> HandlerResult {
        let (d, _) = self.lookup_device(&params)?;
        let Some(s) = d.as_stream_sourcer() else {
            return Err(IpcError::new(
                "not_supported",
                "this driver has no minted stream URL",
            ));
        };
        let url = s
            .stream_url(&CancelToken::new())
            .await
            .map_err(|e| IpcError::new("stream_url_failed", e.message()))?;
        Ok(json!({"url": url}).into())
    }

    /// `hangupHandler`: a driver that answered the call itself and can end it does; for every
    /// other call it is what it always was, `not_implemented` (§9.5).
    pub(super) async fn hangup(&self, params: Params) -> HandlerResult {
        let v =
            gojson::decode(raw_str(&params), "hangupParams", &HANGUP_PARAMS).map_err(bad_params)?;
        let (call_id, reason) = (gojson::str_at(&v, 0), gojson::str_at(&v, 1));
        for d in self.all() {
            let Some(h) = d.as_call_hangupper() else {
                continue;
            };
            let r = match d.as_reasoned_hangupper() {
                Some(rh) => rh.hangup_call_why(&call_id, &reason), // who asked is kept for diagnostics
                None => h.hangup_call_async(&call_id).await,
            };
            match r {
                Ok(()) => return Ok(json!({"ok": true}).into()),
                Err(e) if e.is(Sentinel::NoSuchCall) => continue,
                Err(e) => return Err(IpcError::new("hangup_failed", e.message())),
            }
        }
        Err(IpcError::new("not_implemented", NOT_IMPLEMENTED_MESSAGE))
    }
}
