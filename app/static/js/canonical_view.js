/*
 * CanonicalView — the journey page's display model, built from CanonicalJourney JSON (#303).
 *
 * The fanout API sends canonical journeys (docs/schemas/canonical-journey-v1.schema.json).
 * The page's rendering code reads a flat display model; this file is the one place that turns
 * the first into the second, so no rendering code reads an engine's own fields. The display
 * fields keep the names the rendering code already uses, each with one meaning for every
 * engine:
 *
 *   mode                 a ride's mode family (RAIL, BUS, TRAM…); WALK (or BIKE, CAR) otherwise
 *   departure, arrival   UTC instants as the API writes them (…Z), the same form for every engine
 *   route_short_name     the line, else the label the source shows (the labels shown before)
 *   route_long_name      the long route name, else the source's own label when it differs,
 *                        else the product category's name
 *   agency_name / _id    the operator's name / reference
 *   feed_id              the VIATOR feed of a ride; for a reference engine, its name
 *   first_transit_leg_departure_utc   the first ride's boarding time
 *
 * `_canonical` keeps the journey as received: the JSON inspector shows it (design D3: the
 * engines' raw answers are no longer sent to the page).
 */
(function (root) {
  'use strict';

  // Display names of the reference engines, for their legs' badge.
  const REFERENCE_LABEL = { hafas: 'ÖBB HAFAS', ojp: 'Swiss OJP' };

  function instant(timing) {
    return timing ? timing.timetabled : null;
  }

  function coord(end, key) {
    return end && end.position ? end.position[key] : null;
  }

  function stopRef(end) {
    return end && end.stop_place ? end.stop_place.ref : null;
  }

  function providerOf(journey) {
    return journey && journey.source ? journey.source.provider : null;
  }

  function feedOf(service, journey) {
    if (service && service.feed) return service.feed;
    return REFERENCE_LABEL[providerOf(journey)] || null;
  }

  // The line first, then the label the source shows: the labels the page has always shown
  // (ÖBB `RJ`, MOTIS `S7`, OJP `IC1`), so the side-by-side's identifier note compares what it
  // compared before #303. Comparing labels properly is the matching engine's job (#304).
  function serviceLabel(service) {
    return service.line || service.published_name || null;
  }

  // The long route name; else the published label when the short one is the line (ÖBB
  // `RJ 1141` beside `RJ`); else the product category's name (OJP `InterCity`).
  function longName(service) {
    const category = service.product_category;
    const published = service.published_name;
    const extraLabel = published && published !== serviceLabel(service) ? published : null;
    return service.route_long_name || extraLabel || (category && category.name) || null;
  }

  function legMode(canonicalLeg) {
    if (canonicalLeg.kind === 'timed') {
      return canonicalLeg.service ? canonicalLeg.service.mode : null;
    }
    return canonicalLeg.continuous_mode || 'WALK';
  }

  function end(prefix, place) {
    const out = {};
    out[prefix + '_name'] = (place && place.name) || null;
    out[prefix + '_lat'] = coord(place, 'lat');
    out[prefix + '_lon'] = coord(place, 'lon');
    out[prefix + '_stop_id'] = stopRef(place);
    return out;
  }

  function leg(canonicalLeg, journey) {
    const service = canonicalLeg.service || null;
    const operator = service && service.operator ? service.operator : null;
    return Object.assign(
      {
        kind: canonicalLeg.kind,
        mode: legMode(canonicalLeg),
        departure: instant(canonicalLeg.departure),
        arrival: instant(canonicalLeg.arrival),
        duration_seconds: canonicalLeg.duration_seconds,
        distance_meters: canonicalLeg.distance_meters || 0,
      },
      end('from', canonicalLeg.origin),
      end('to', canonicalLeg.destination),
      {
        route_short_name: service ? serviceLabel(service) : null,
        route_long_name: service ? longName(service) : null,
        agency_name: operator ? operator.name || null : null,
        agency_id: operator ? operator.ref || null : null,
        feed_id: feedOf(service, journey),
        trip_headsign: service ? service.destination_text || null : null,
      }
    );
  }

  function journey(canonical) {
    if (!canonical) return canonical;
    const rides = (canonical.legs || []).filter((l) => l.kind === 'timed');
    const families = rides.map((l) => l.service && l.service.mode).filter(Boolean);
    const source = canonical.source || {};
    return {
      id: canonical.id,
      provider: source.provider || null,
      duration_seconds: canonical.duration_seconds,
      num_transfers:
        canonical.source_transfer_count == null
          ? Math.max(0, rides.length - 1)
          : canonical.source_transfer_count,
      departure_at: instant(canonical.departure) || '',
      arrival_at: instant(canonical.arrival) || '',
      modes: Array.from(new Set(families)).sort().join(','),
      legs: (canonical.legs || []).map((l) => leg(l, canonical)),
      first_transit_leg_departure_utc: rides.length ? instant(rides[0].departure) : null,
      via_hubs: source.via_hubs || [],
      stitched_from_sessions: source.stitched_from_sessions || [],
      _canonical: canonical,
    };
  }

  // A reference engine's answer: its journeys as display models, each with the comparison
  // tag the server gave it by journey id (`comparison`, OJP only).
  function reference(answer) {
    const tags = answer.comparison || {};
    const trips = (answer.trips || []).map((canonical) => {
      const view = journey(canonical);
      if (tags[canonical.id]) view.comparison = tags[canonical.id];
      return view;
    });
    return Object.assign({}, answer, { trips: trips });
  }

  // The fanout answer with every journey as a display model; everything else as received.
  function fromFanout(payload) {
    const out = Object.assign({}, payload);
    out.trips = (payload.trips || []).map((slot) =>
      Object.assign({}, slot, { best: journey(slot.best) })
    );
    if (payload.ojp_reference) out.ojp_reference = reference(payload.ojp_reference);
    if (payload.hafas_reference) out.hafas_reference = reference(payload.hafas_reference);
    if (payload.federated_trips) out.federated_trips = payload.federated_trips.map(journey);
    return out;
  }

  root.CanonicalView = { fromFanout: fromFanout, journey: journey, leg: leg };
})(globalThis);
