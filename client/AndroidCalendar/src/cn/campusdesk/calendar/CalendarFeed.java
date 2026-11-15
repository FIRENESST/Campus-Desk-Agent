package cn.campusdesk.calendar;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.StringReader;
import java.net.URI;
import java.net.URISyntaxException;
import java.time.DateTimeException;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.ResolverStyle;
import java.util.ArrayDeque;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

final class CalendarFeed {
    private static final DateTimeFormatter UTC = DateTimeFormatter.ofPattern("uuuuMMdd'T'HHmmss'Z'", Locale.ROOT)
        .withResolverStyle(ResolverStyle.STRICT);

    static URI link(String value) throws IOException {
        try {
            URI uri = new URI(value.trim());
            if (value.length() <= 2048 && "https".equalsIgnoreCase(uri.getScheme()) && uri.getHost() != null
                    && uri.getRawUserInfo() == null && uri.getRawQuery() == null && uri.getRawFragment() == null
                    && uri.getRawPath().matches("/calendar/cal_[A-Za-z0-9_-]{43}\\.ics")) {
                return uri;
            }
        } catch (URISyntaxException ignored) {
        }
        throw new IOException("请粘贴在校园助手创建的完整 HTTPS 个人订阅链接。");
    }

    static final class Event {
        final String uid;
        final String title;
        final String location;
        final String category;
        final long start;
        final long end;

        Event(Map<String, String> values) throws IOException {
            uid = values.get("UID");
            title = unescape(values.getOrDefault("SUMMARY", ""));
            location = unescape(values.getOrDefault("LOCATION", ""));
            category = unescape(values.getOrDefault("CATEGORIES", ""));
            try {
                start = instant(values.getOrDefault("DTSTART", ""));
                end = instant(values.getOrDefault("DTEND", ""));
            } catch (DateTimeException failure) {
                throw invalid();
            }
            if (uid == null || uid.trim().isEmpty() || title.trim().isEmpty() || end <= start) throw invalid();
        }
    }

    static Map<String, Event> parse(String input) throws IOException {
        Parser parser = new Parser();
        try (BufferedReader reader = new BufferedReader(new StringReader(input))) {
            StringBuilder logical = new StringBuilder();
            for (String line; (line = reader.readLine()) != null;) {
                if (line.startsWith(" ") || line.startsWith("\t")) {
                    if (logical.length() == 0) throw invalid();
                    logical.append(line.substring(1));
                } else {
                    if (logical.length() > 0) parser.read(logical.toString());
                    logical.setLength(0);
                    logical.append(line);
                }
                if (logical.length() > 4096) throw invalid();
            }
            if (logical.length() > 0) parser.read(logical.toString());
        }
        if (!parser.closed || !parser.version) throw invalid();
        return parser.events;
    }

    private static final class Parser {
        final ArrayDeque<String> components = new ArrayDeque<>();
        final Map<String, Event> events = new LinkedHashMap<>();
        Map<String, String> fields;
        boolean version;
        boolean closed;

        void read(String line) throws IOException {
            int divider = line.indexOf(':');
            if (closed || divider < 1 || line.indexOf('\0') >= 0) throw invalid();
            String name = line.substring(0, divider);
            String value = line.substring(divider + 1);
            String component = components.peek();
            switch (name) {
                case "BEGIN":
                    if (!validChild(component, value)) throw invalid();
                    components.push(value);
                    if ("VEVENT".equals(value)) fields = new LinkedHashMap<>();
                    break;
                case "END":
                    if (!value.equals(components.poll())) throw invalid();
                    if ("VEVENT".equals(value)) {
                        Event entry = new Event(fields);
                        if (events.putIfAbsent(entry.uid, entry) != null) throw invalid();
                    }
                    closed = components.isEmpty();
                    break;
                case "VERSION":
                    if (!"VCALENDAR".equals(component) || version || !"2.0".equals(value)) throw invalid();
                    version = true;
                    break;
                default:
                    if (component == null) throw invalid();
                    if ("VEVENT".equals(component) && fields.putIfAbsent(name, value) != null) throw invalid();
            }
        }

        private boolean validChild(String parent, String child) {
            if (parent == null) return "VCALENDAR".equals(child);
            if ("VCALENDAR".equals(parent)) return "VEVENT".equals(child) || "VTIMEZONE".equals(child);
            return "VTIMEZONE".equals(parent) && ("STANDARD".equals(child) || "DAYLIGHT".equals(child));
        }
    }

    private static long instant(String value) {
        return LocalDateTime.parse(value, UTC).toInstant(ZoneOffset.UTC).toEpochMilli();
    }

    private static String unescape(String value) throws IOException {
        StringBuilder output = new StringBuilder(value.length());
        for (int i = 0; i < value.length(); i++) {
            char character = value.charAt(i);
            if (character == '\\') {
                if (++i == value.length()) throw invalid();
                char next = value.charAt(i);
                if (next == 'n' || next == 'N') character = '\n';
                else if (next == '\\' || next == ',' || next == ';') character = next;
                else throw invalid();
            }
            output.append(character);
        }
        return output.toString();
    }

    private static IOException invalid() {
        return new IOException("订阅返回的日程格式有误，已保留手机里的日程。");
    }
}
