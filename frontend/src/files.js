// Rule file names.

/** A name the integration loads (`<config>/homeo/*.homeo`): not hidden, no folders. Matches
 * the check in custom_components/homeo/websocket.py. */
export function validFileName(name) {
  return name.endsWith(".homeo") && name !== ".homeo" && !name.startsWith(".") && !/[/\\\0]/.test(name);
}
