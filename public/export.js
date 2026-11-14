"use strict";

// Small, dependency-free writers for the downloads: animated GIF and stored ZIP.

const Export = (() => {
  // -------------------------------------------------------------------------
  // GIF89a: one global palette, LZW-compressed frames, looping forever.
  // -------------------------------------------------------------------------
  const TRANSPARENT = 255;

  // Up to 255 opaque colours; transparent pixels (alpha < 128) take index 255.
  // If the frames hold more colours, drop low bits until they fit.
  function palette(frames) {
    for (let shift = 0; shift < 8; shift++) {
      const index = new Map();
      let fits = true;
      outer: for (const f of frames) {
        const d = f.data;
        for (let i = 0; i < d.length; i += 4) {
          if (d[i + 3] < 128) continue;
          const key = ((d[i] >> shift) << 16) | ((d[i + 1] >> shift) << 8) | (d[i + 2] >> shift);
          if (!index.has(key)) {
            if (index.size === 255) {
              fits = false;
              break outer;
            }
            index.set(key, index.size);
          }
        }
      }
      if (fits) return { shift, index };
    }
    throw new Error("unreachable: 8-bit shift leaves one colour");
  }

  function lzw(indices, minCodeSize) {
    // Mirrors omggif's encoder: grow the code size before inserting, clear at 4096.
    const clear = 1 << minCodeSize;
    const eoi = clear + 1;
    const out = [];
    let codeSize = minCodeSize + 1;
    let next = eoi + 1;
    let table = new Map();
    let cur = 0;
    let bits = 0;
    const emit = (code) => {
      cur |= code << bits;
      bits += codeSize;
      while (bits >= 8) {
        out.push(cur & 0xff);
        cur >>>= 8;
        bits -= 8;
      }
    };
    emit(clear);
    let prefix = indices[0];
    for (let i = 1; i < indices.length; i++) {
      const k = indices[i];
      const key = (prefix << 8) | k;
      const code = table.get(key);
      if (code !== undefined) {
        prefix = code;
        continue;
      }
      emit(prefix);
      if (next === 4096) {
        emit(clear);
        next = eoi + 1;
        codeSize = minCodeSize + 1;
        table = new Map();
      } else {
        if (next >= 1 << codeSize) codeSize++;
        table.set(key, next++);
      }
      prefix = k;
    }
    emit(prefix);
    emit(eoi);
    if (bits > 0) out.push(cur & 0xff);
    return out;
  }

  function gif(frames, delayMs) {
    const w = frames[0].width;
    const h = frames[0].height;
    const { shift, index } = palette(frames);
    const bytes = [];
    const u16 = (v) => bytes.push(v & 0xff, (v >> 8) & 0xff);
    const ascii = (s) => [...s].forEach((c) => bytes.push(c.charCodeAt(0)));

    ascii("GIF89a");
    u16(w);
    u16(h);
    bytes.push(0xf7, TRANSPARENT, 0); // global table of 256 colours
    const table = new Uint8Array(256 * 3);
    const half = shift ? 1 << (shift - 1) : 0;
    for (const [key, i] of index) {
      table[3 * i] = Math.min(255, (((key >> 16) & 0xff) << shift) + half);
      table[3 * i + 1] = Math.min(255, (((key >> 8) & 0xff) << shift) + half);
      table[3 * i + 2] = Math.min(255, ((key & 0xff) << shift) + half);
    }
    bytes.push(...table);
    bytes.push(0x21, 0xff, 0x0b);
    ascii("NETSCAPE2.0");
    bytes.push(0x03, 0x01, 0x00, 0x00, 0x00); // loop forever

    const delay = Math.max(2, Math.round(delayMs / 10));
    for (const f of frames) {
      bytes.push(0x21, 0xf9, 0x04, 0x09); // restore to background, has transparency
      u16(delay);
      bytes.push(TRANSPARENT, 0x00);
      bytes.push(0x2c);
      u16(0);
      u16(0);
      u16(w);
      u16(h);
      bytes.push(0x00);
      const d = f.data;
      const px = new Uint8Array(w * h);
      for (let i = 0, p = 0; p < px.length; i += 4, p++) {
        px[p] =
          d[i + 3] < 128
            ? TRANSPARENT
            : index.get(((d[i] >> shift) << 16) | ((d[i + 1] >> shift) << 8) | (d[i + 2] >> shift));
      }
      bytes.push(8);
      const data = lzw(px, 8);
      for (let i = 0; i < data.length; i += 255) {
        const block = data.slice(i, i + 255);
        bytes.push(block.length, ...block);
      }
      bytes.push(0x00);
    }
    bytes.push(0x3b);
    return new Blob([new Uint8Array(bytes)], { type: "image/gif" });
  }

  // -------------------------------------------------------------------------
  // ZIP, stored (no compression): PNGs are already compressed.
  // -------------------------------------------------------------------------
  const CRC_TABLE = (() => {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      t[n] = c >>> 0;
    }
    return t;
  })();

  function crc32(bytes) {
    let c = 0xffffffff;
    for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  }

  function zip(files) {
    const parts = [];
    const central = [];
    let offset = 0;
    const DOS_DATE = (2026 - 1980) << 9 | (1 << 5) | 1; // a fixed date: 2026-01-01
    for (const { name, bytes } of files) {
      const nameBytes = new TextEncoder().encode(name);
      const crc = crc32(bytes);
      const local = new DataView(new ArrayBuffer(30));
      local.setUint32(0, 0x04034b50, true);
      local.setUint16(4, 20, true);
      local.setUint16(12, DOS_DATE, true);
      local.setUint32(14, crc, true);
      local.setUint32(18, bytes.length, true);
      local.setUint32(22, bytes.length, true);
      local.setUint16(26, nameBytes.length, true);
      parts.push(new Uint8Array(local.buffer), nameBytes, bytes);

      const entry = new DataView(new ArrayBuffer(46));
      entry.setUint32(0, 0x02014b50, true);
      entry.setUint16(4, 20, true);
      entry.setUint16(6, 20, true);
      entry.setUint16(14, DOS_DATE, true);
      entry.setUint32(16, crc, true);
      entry.setUint32(20, bytes.length, true);
      entry.setUint32(24, bytes.length, true);
      entry.setUint16(28, nameBytes.length, true);
      entry.setUint32(42, offset, true);
      central.push(new Uint8Array(entry.buffer), nameBytes);
      offset += 30 + nameBytes.length + bytes.length;
    }
    const size = central.reduce((s, b) => s + b.length, 0);
    const end = new DataView(new ArrayBuffer(22));
    end.setUint32(0, 0x06054b50, true);
    end.setUint16(8, files.length, true);
    end.setUint16(10, files.length, true);
    end.setUint32(12, size, true);
    end.setUint32(16, offset, true);
    return new Blob([...parts, ...central, new Uint8Array(end.buffer)], { type: "application/zip" });
  }

  function save(blob, name) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  return { gif, zip, save, crc32 };
})();
