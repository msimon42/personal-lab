"""Source adapters. Each declares an id, version and schema fingerprint and fails closed
into review when a source layout changes. Parsing never publishes; normalization never
repairs arithmetic or turns missing values into zero."""
