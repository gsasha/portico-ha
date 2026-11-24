//! One newline framer for the three places that read lines (R2-44): the request loop of
//! [`super::Server`], the TCP auth line, and the `-secrets-env` file reader.
//!
//! It is `bufio.Scanner` with `ScanLines` and a 1 MiB buffer, as Go uses it: a line is the bytes
//! up to `\n` minus one trailing `\r`; a line plus its newline must fit in [`MAX_LINE`] bytes;
//! a partial line that fills the buffer without a newline is too long. What it does not copy
//! from Go is the cost: it remembers where it stopped looking, so a 1 MiB line that arrives in
//! small pieces is scanned once, not once per piece.

use super::MAX_LINE;

/// What [`LineScanner::next_line`] found.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Scan {
    /// a complete line (no `\n`, no `\r` before it)
    Line(Vec<u8>),
    /// no newline yet: [`LineScanner::push`] more
    More,
    /// `bufio.ErrTooLong`
    TooLong,
}

#[derive(Default)]
pub(crate) struct LineScanner {
    buf: Vec<u8>,
    /// where the next line starts
    start: usize,
    /// where the search for a newline resumes (never before `start`)
    scanned: usize,
}

impl LineScanner {
    pub(crate) fn new() -> LineScanner {
        LineScanner::default()
    }

    /// A scanner that starts with bytes already read.
    pub(crate) fn with_buffered(buf: Vec<u8>) -> LineScanner {
        LineScanner {
            buf,
            start: 0,
            scanned: 0,
        }
    }

    pub(crate) fn push(&mut self, bytes: &[u8]) {
        self.buf.extend_from_slice(bytes);
    }

    pub(crate) fn next_line(&mut self) -> Scan {
        match self.buf[self.scanned..].iter().position(|b| *b == b'\n') {
            Some(rel) => {
                let end = self.scanned + rel;
                if end - self.start + 1 > MAX_LINE {
                    return Scan::TooLong;
                }
                let mut line = &self.buf[self.start..end];
                if line.last() == Some(&b'\r') {
                    line = &line[..line.len() - 1];
                }
                let line = line.to_vec();
                self.start = end + 1;
                self.scanned = self.start;
                Scan::Line(line)
            }
            None => {
                self.scanned = self.buf.len();
                if self.start > 0 {
                    self.buf.drain(..self.start);
                    self.scanned -= self.start;
                    self.start = 0;
                }
                if self.buf.len() >= MAX_LINE {
                    Scan::TooLong
                } else {
                    Scan::More
                }
            }
        }
    }

    /// At end of input: the unterminated last line, if any (a final line with no newline is
    /// still a line), minus one trailing `\r`.
    pub(crate) fn finish(&mut self) -> Option<Vec<u8>> {
        let mut tail = self.take_rest();
        if tail.is_empty() {
            return None;
        }
        if tail.last() == Some(&b'\r') {
            tail.pop();
        }
        Some(tail)
    }

    /// Everything buffered and not yet returned as a line (what a client sent after its auth
    /// line).
    pub(crate) fn take_rest(&mut self) -> Vec<u8> {
        self.buf.drain(..self.start);
        self.start = 0;
        self.scanned = 0;
        std::mem::take(&mut self.buf)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lines_split_on_newline_and_lose_one_carriage_return() {
        let mut s = LineScanner::new();
        s.push(b"a\r\nb\r\r\n\nc");
        assert_eq!(s.next_line(), Scan::Line(b"a".to_vec()));
        assert_eq!(s.next_line(), Scan::Line(b"b\r".to_vec()));
        assert_eq!(s.next_line(), Scan::Line(Vec::new()));
        assert_eq!(s.next_line(), Scan::More);
        assert_eq!(s.finish(), Some(b"c".to_vec()));
        assert_eq!(s.finish(), None);
    }

    #[test]
    fn a_line_in_pieces_is_scanned_once() {
        let mut s = LineScanner::new();
        for _ in 0..1000 {
            s.push(&[b'x'; 100]);
            assert_eq!(s.next_line(), Scan::More);
            // the search resumed after the bytes it had seen
            assert_eq!(s.scanned, s.buf.len());
        }
        s.push(b"\nrest");
        assert_eq!(s.next_line(), Scan::Line(vec![b'x'; 100_000]));
        assert_eq!(s.take_rest(), b"rest".to_vec());
    }

    #[test]
    fn the_limit_is_the_scanner_buffer() {
        // a line of MAX_LINE - 1 bytes plus its newline fits; one more does not
        let mut s = LineScanner::new();
        s.push(&vec![b'a'; MAX_LINE - 1]);
        s.push(b"\n");
        assert!(matches!(s.next_line(), Scan::Line(l) if l.len() == MAX_LINE - 1));
        let mut s = LineScanner::new();
        s.push(&vec![b'a'; MAX_LINE]);
        s.push(b"\n");
        assert_eq!(s.next_line(), Scan::TooLong);
        // no newline: too long as soon as the buffer is full
        let mut s = LineScanner::new();
        s.push(&vec![b'a'; MAX_LINE - 1]);
        assert_eq!(s.next_line(), Scan::More);
        s.push(b"a");
        assert_eq!(s.next_line(), Scan::TooLong);
    }
}
