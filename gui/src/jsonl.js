'use strict';

/**
 * Builds a feeder for a JSON-lines stream.
 *
 * Chunks may split a line or carry several; a line is handled only once its
 * newline arrives. Blank lines are skipped. A line that is not a JSON object
 * goes to `onBadLine`.
 *
 * @param {(obj: object) => void} onObject called once per decoded object
 * @param {(line: string) => void} onBadLine called with each undecodable line
 * @returns {(chunk: string) => void} feed function
 */
function createLineDecoder(onObject, onBadLine) {
  let buffer = '';
  return function feed(chunk) {
    buffer += chunk;
    let nl = buffer.indexOf('\n');
    while (nl !== -1) {
      let line = buffer.slice(0, nl);
      buffer = buffer.slice(nl + 1);
      if (line.endsWith('\r')) line = line.slice(0, -1);
      if (line.trim() !== '') {
        let obj;
        try {
          obj = JSON.parse(line);
        } catch {
          obj = undefined;
        }
        if (obj !== null && typeof obj === 'object' && !Array.isArray(obj)) {
          onObject(obj);
        } else {
          onBadLine(line);
        }
      }
      nl = buffer.indexOf('\n');
    }
  };
}

module.exports = { createLineDecoder };
