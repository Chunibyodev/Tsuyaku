// Settings shared by the popup, the background page and the YouTube scripts (browser.storage.local).
// eslint-disable-next-line no-unused-vars
const TSY_DEFAULTS = {
  subtitles: true,      // English subtitles on YouTube's player
  showJp: false,        // Japanese line above every subtitle (or click a subtitle)
  subSize: 100,         // subtitle size, percent
  chatTranslate: true,  // translate YouTube's chat in place
  messageBox: true,     // English -> Japanese suggestions in YouTube's chat box
  tone: 'casual',       // casual / polite / fan
  autoStart: false,     // switch on when Firefox starts
};

// eslint-disable-next-line no-unused-vars
async function tsyPrefs() {
  try {
    return Object.assign({}, TSY_DEFAULTS, await browser.storage.local.get(Object.keys(TSY_DEFAULTS)));
  } catch (e) {
    return Object.assign({}, TSY_DEFAULTS);
  }
}
