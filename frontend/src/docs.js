// Short documentation for the language's builtins, keywords and operators, shown on hover and
// in completion. The wording follows docs/design.md. Tests keep it complete: the highlighter's
// builtin lists must match the checker's (Python), and each must have an entry here (JS).

const DAY_NAMES = {
  mon: "Monday",
  tue: "Tuesday",
  wed: "Wednesday",
  thu: "Thursday",
  fri: "Friday",
  sat: "Saturday",
  sun: "Sunday",
};

const SOLAR = {
  sunrise: ["sunrise", "next_rising"],
  sunset: ["sunset", "next_setting"],
  dawn: ["dawn (when the sun is 6° below the horizon, before sunrise)", "next_dawn"],
  dusk: ["dusk (when the sun is 6° below the horizon, after sunset)", "next_dusk"],
};

/** `sun.sun`'s attribute with the next occurrence of each solar time. */
export const SOLAR_ATTRS = Object.fromEntries(Object.entries(SOLAR).map(([k, [, a]]) => [k, a]));

/** Entries: `sig` (how it's written), `kind`, `text` (backticks mark code), `example`. */
export const DOCS = {
  // ---------------------------------------------------------------- functions
  age: {
    sig: "age(x)",
    kind: "function",
    text:
      "How long since `x` last changed, as a duration. For buttons and events, the time since " +
      "the last press or event. Works on any expression, like `age(co2 > 800)`.",
    example: "age(input_button.boost) < 30min",
  },
  fresh: {
    sig: "fresh(x)",
    kind: "function",
    text:
      "Only inside a `state` block: whether `x` happened after the state machine last changed " +
      "(a button pressed, an event fired, a time passed or a condition became true since it " +
      "entered its current state).",
    example: 'fresh(input_button.goodnight) -> "night"',
  },
  previous: {
    sig: "previous(x)",
    kind: "function",
    text:
      "The value `x` had before it last changed. After a restart it is `unknown` until `x` " +
      "changes again.",
    example: "fresh(input_button.movie) -> previous(house)",
  },
  toggle: {
    sig: "toggle(x, initial: off)",
    kind: "function",
    text:
      "Flips between `on` and `off` each time `x` happens: a button press, an event, or a " +
      "condition becoming true. It starts from `initial` (`off` if left out), also after a " +
      "restart.",
    example: "light.desk := toggle(input_button.desk)",
  },
  count: {
    sig: "count(x, duration)",
    kind: "function",
    text:
      "How many times `x` happened in the last `duration`, e.g. to detect a triple press. It " +
      "only counts what happened since Home Assistant started.",
    example: "count(event.remote.press, 2s) >= 3",
  },
  manual: {
    sig: "manual(entity)",
    kind: "function",
    text:
      "When `entity` was last changed by something other than homeo: a person, a physical " +
      "switch, the device itself or another automation. It's a timestamp, so use it with " +
      "`age()`, `latest` or `on`.",
    example: "age(manual(fan.bathroom)) < 1h -> keep",
  },
  available: {
    sig: "available(x)",
    kind: "function",
    text: "Whether `x` has a known value now: false while it's `unknown` or `unavailable`.",
    example: "let co2_ok = available(sensor.co2)",
  },

  // ---------------------------------------------------------------- names
  now: {
    sig: "now",
    kind: "timestamp",
    text: "The current time. Compare it with times of day, or test it against a range of them.",
    example: "now in 22:00..06:00",
  },
  today: {
    sig: "today",
    kind: "day",
    text: "Today's day of the week. Compare it with days, or test it against a range or list.",
    example: "today in mon..fri",
  },
  ...Object.fromEntries(
    Object.entries(SOLAR).map(([name, [what]]) => [
      name,
      {
        sig: name,
        kind: "solar time",
        text:
          `Today's ${what}, from \`sun.sun\`. A time of day in comparisons and ranges, and a ` +
          `time point elsewhere: \`on ${name}:\`, \`fresh(${name})\`, \`age(${name})\`. Add or ` +
          `subtract durations to shift it.`,
        example: name === "sunset" ? "now in sunset - 30min..23:00" : `on ${name} + 15min:`,
      },
    ]),
  ),
  ...Object.fromEntries(
    Object.entries(DAY_NAMES).map(([name, day]) => [
      name,
      { sig: name, kind: "day", text: `${day}. Compare it with \`today\`.`, example: `today == ${name}` },
    ]),
  ),
  weekday: {
    sig: "weekday",
    kind: "days",
    text: "Monday to Friday. Test `today` against it with `in`.",
    example: "today in weekday",
  },
  weekend: {
    sig: "weekend",
    kind: "days",
    text: "Saturday and Sunday. Test `today` against it with `in`.",
    example: "today in weekend",
  },

  // ---------------------------------------------------------------- keywords
  let: {
    sig: "let name = expression",
    kind: "keyword",
    text:
      "Names a value: an alias for an entity, or anything computed. Each file has its own names.",
    example: "let stuffy = co2 > 800 or humidity > 70%",
  },
  state: {
    sig: 'state name = "initial":',
    kind: "keyword",
    text:
      "A state machine: a named value that remembers. Its arms compute the next state from the " +
      "current one, first match wins; if none matches, it stays. Home Assistant shows it as " +
      "`select.homeo_<name>`.",
    example: 'state mode = "auto":\n    fresh(input_button.boost) -> "boost"',
  },
  on: {
    sig: "on x:",
    kind: "action",
    text:
      "Runs the service calls below each time `x` happens: a button press, an event, a time, or " +
      "a condition becoming true. `on x if g:` runs only if `g` is true at that moment.",
    example: 'on input_button.doorbell:\n    notify.phone(message: "Someone\'s at the door")',
  },
  "on(": {
    sig: "on(setting: value, …)",
    kind: "value",
    text:
      "Turns the device on with these settings: `brightness` and `kelvin` for lights, " +
      "`percentage` for fans.",
    example: "light.hall := on(brightness: 50%, kelvin: 2700)",
  },
  keep: {
    sig: "keep",
    kind: "value",
    text:
      "In a binding: leave the device as it is, e.g. while someone has changed it by hand. " +
      "When the arm stops applying, the device is brought back in line.",
    example: "age(manual(light.hall)) < 1h -> keep",
  },
  for: {
    sig: "x for duration",
    kind: "keyword",
    text: "`x` has been true for at least `duration`. Short for `x and age(x) >= duration`.",
    example: "light.porch := lux < 20 for 5min",
  },
  within: {
    sig: "x within duration",
    kind: "keyword",
    text:
      "`x` has been true at some point in the last `duration`: it's true now, or stopped being " +
      "true less than `duration` ago. Short for `x or age(x) < duration`.",
    example: "light.hall := motion within 5min",
  },
  latest: {
    sig: "latest",
    kind: "keyword",
    text:
      "A block of arms where the condition that happened most recently wins: the last button " +
      "pressed, or the condition that last became true. `else` applies while none has happened " +
      "yet (in a binding it may be left out, and then means `keep`).",
    example: "light.desk := latest\n    input_button.desk_on  -> on\n    input_button.desk_off -> off",
  },
  every: {
    sig: "every duration",
    kind: "keyword",
    text:
      "A time point that happens every `duration`, counted from local midnight: `every 15min` " +
      "happens at :00, :15, :30 and :45.",
    example: "on every 1h:",
  },
  in: {
    sig: "x in a..b",
    kind: "keyword",
    text:
      "Whether `x` is in a range of times or days (`22:00..06:00` wraps past midnight), in a " +
      'set of days like `weekend`, or in a list like `["heating", "idle"]`.',
    example: "now in sunset..23:00",
  },
  if: {
    sig: "on x if g:   or   a if c else b",
    kind: "keyword",
    text:
      "In an action header, run only if `g` is true when `x` happens (unlike `and`, `g` becoming " +
      "true later doesn't run it). In an expression, `a` while `c` is true, else `b`.",
    example: 'on person.ann == "home" if night:',
  },
  else: {
    sig: "else -> result",
    kind: "keyword",
    text:
      "The arm that applies when no other does. A binding's cascade needs one; in `latest` it " +
      "applies while no condition has happened yet. Also part of `a if c else b`.",
    example: "else -> stuffy",
  },

  // ---------------------------------------------------------------- operators
  ":=": {
    sig: "entity := value",
    kind: "binding",
    text:
      "Keeps the entity on the left in line with the value on the right. It writes when the " +
      "value changes, so a change made by hand sticks until then.",
    example: "fan.bathroom := stuffy",
  },
  "->": {
    sig: "condition -> result",
    kind: "arm",
    text:
      "Arms are tried top to bottom and the first whose condition is true gives the result " +
      "(in `latest`, the one that happened most recently). An `unknown` condition falls through.",
    example: "binary_sensor.window -> off",
  },
  "|": {
    sig: "guard\n    | condition -> result",
    kind: "sub-arm",
    text:
      "Sub-arms apply only while the guard above them is true. If none matches, evaluation " +
      "continues after the group.",
    example: "motion\n    | night -> on(brightness: 10%)\n    | else  -> on",
  },
  "??": {
    sig: "x ?? fallback",
    kind: "operator",
    text: "`x`, or `fallback` while `x` is `unknown` (e.g. a sensor is unavailable).",
    example: "sensor.co2 ?? 0",
  },
  "..": {
    sig: "a..b",
    kind: "range",
    text: "A range of times or days, for `in`. Times wrap past midnight: `22:00..06:00`.",
    example: "today in mon..fri",
  },
};

/** The entry as plain text, for completion info. */
export function docText(entry) {
  const plain = (s) => s.replace(/`([^`]+)`/g, "$1");
  return `${entry.sig}\n\n${plain(entry.text)}\n\ne.g. ${entry.example}`;
}
