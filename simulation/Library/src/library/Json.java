package library;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class Json {
    private static final int MAX_DEPTH = 64;

    private Json() {
    }

    public static Object parse(String input) {
        if (input == null) {
            throw new IllegalArgumentException("JSON 不能为空");
        }
        Parser parser = new Parser(input);
        Object value = parser.parseValue(0);
        parser.skipWhitespace();
        if (!parser.atEnd()) {
            throw parser.error("JSON 末尾存在多余内容");
        }
        return value;
    }

    public static String stringify(Object value) {
        StringBuilder output = new StringBuilder();
        writeValue(value, output);
        return output.toString();
    }

    private static void writeValue(Object value, StringBuilder output) {
        switch (value) {
            case null -> output.append("null");
            case String text -> writeString(text, output);
            case Boolean bool -> output.append(bool);
            case BigDecimal number -> output.append(number.toString());
            case Number number -> writeNumber(number, output);
            case Map<?, ?> map -> writeObject(map, output);
            case Iterable<?> items -> writeArray(items, output);
            default -> throw new IllegalArgumentException("不支持的 JSON 类型: " + value.getClass());
        }
    }

    private static void writeNumber(Number number, StringBuilder output) {
        if ((number instanceof Double doubleValue && !Double.isFinite(doubleValue))
                || (number instanceof Float floatValue && !Float.isFinite(floatValue))) {
            throw new IllegalArgumentException("JSON 数字必须有限");
        }
        try {
            output.append(new BigDecimal(number.toString()).toString());
        } catch (NumberFormatException exception) {
            throw new IllegalArgumentException("非法 JSON 数字", exception);
        }
    }

    private static void writeObject(Map<?, ?> map, StringBuilder output) {
        output.append('{');
        boolean first = true;
        for (Map.Entry<?, ?> entry : map.entrySet()) {
            if (!(entry.getKey() instanceof String key)) {
                throw new IllegalArgumentException("JSON 对象键必须是字符串");
            }
            if (!first) {
                output.append(',');
            }
            first = false;
            writeString(key, output);
            output.append(':');
            writeValue(entry.getValue(), output);
        }
        output.append('}');
    }

    private static void writeArray(Iterable<?> items, StringBuilder output) {
        output.append('[');
        boolean first = true;
        for (Object item : items) {
            if (!first) {
                output.append(',');
            }
            first = false;
            writeValue(item, output);
        }
        output.append(']');
    }

    private static void writeString(String text, StringBuilder output) {
        output.append('"');
        for (int index = 0; index < text.length(); index++) {
            char character = text.charAt(index);
            switch (character) {
                case '"' -> output.append("\\\"");
                case '\\' -> output.append("\\\\");
                case '\b' -> output.append("\\b");
                case '\f' -> output.append("\\f");
                case '\n' -> output.append("\\n");
                case '\r' -> output.append("\\r");
                case '\t' -> output.append("\\t");
                default -> {
                    if (character < 0x20) {
                        output.append(String.format("\\u%04x", (int) character));
                    } else if (Character.isHighSurrogate(character)) {
                        if (index + 1 >= text.length() || !Character.isLowSurrogate(text.charAt(index + 1))) {
                            throw new IllegalArgumentException("字符串包含孤立高代理项");
                        }
                        output.append(character).append(text.charAt(++index));
                    } else if (Character.isLowSurrogate(character)) {
                        throw new IllegalArgumentException("字符串包含孤立低代理项");
                    } else {
                        output.append(character);
                    }
                }
            }
        }
        output.append('"');
    }

    private static final class Parser {
        private final String input;
        private int index;

        private Parser(String input) {
            this.input = input;
        }

        private Object parseValue(int depth) {
            skipWhitespace();
            if (atEnd()) {
                throw error("JSON 值缺失");
            }
            return switch (input.charAt(index)) {
                case '{' -> parseObject(depth);
                case '[' -> parseArray(depth);
                case '"' -> parseString();
                case 't' -> parseLiteral("true", Boolean.TRUE);
                case 'f' -> parseLiteral("false", Boolean.FALSE);
                case 'n' -> parseLiteral("null", null);
                default -> parseNumber();
            };
        }

        private Map<String, Object> parseObject(int depth) {
            checkDepth(depth);
            index++;
            LinkedHashMap<String, Object> object = new LinkedHashMap<>();
            skipWhitespace();
            if (consume('}')) {
                return object;
            }
            while (true) {
                skipWhitespace();
                if (atEnd() || input.charAt(index) != '"') {
                    throw error("对象键必须是字符串");
                }
                String key = parseString();
                skipWhitespace();
                require(':');
                Object value = parseValue(depth + 1);
                if (object.containsKey(key)) {
                    throw error("对象键重复: " + key);
                }
                object.put(key, value);
                skipWhitespace();
                if (consume('}')) {
                    return object;
                }
                require(',');
            }
        }

        private List<Object> parseArray(int depth) {
            checkDepth(depth);
            index++;
            List<Object> array = new ArrayList<>();
            skipWhitespace();
            if (consume(']')) {
                return array;
            }
            while (true) {
                array.add(parseValue(depth + 1));
                skipWhitespace();
                if (consume(']')) {
                    return array;
                }
                require(',');
            }
        }

        private String parseString() {
            require('"');
            StringBuilder value = new StringBuilder();
            while (!atEnd()) {
                char character = input.charAt(index++);
                if (character == '"') {
                    return value.toString();
                }
                if (character == '\\') {
                    appendEscape(value);
                } else if (character < 0x20) {
                    throw error("字符串包含未转义控制字符");
                } else if (Character.isHighSurrogate(character)) {
                    if (atEnd() || !Character.isLowSurrogate(input.charAt(index))) {
                        throw error("字符串包含孤立高代理项");
                    }
                    value.append(character).append(input.charAt(index++));
                } else if (Character.isLowSurrogate(character)) {
                    throw error("字符串包含孤立低代理项");
                } else {
                    value.append(character);
                }
            }
            throw error("字符串未闭合");
        }

        private void appendEscape(StringBuilder value) {
            if (atEnd()) {
                throw error("字符串转义未完成");
            }
            char escaped = input.charAt(index++);
            switch (escaped) {
                case '"' -> value.append('"');
                case '\\' -> value.append('\\');
                case '/' -> value.append('/');
                case 'b' -> value.append('\b');
                case 'f' -> value.append('\f');
                case 'n' -> value.append('\n');
                case 'r' -> value.append('\r');
                case 't' -> value.append('\t');
                case 'u' -> appendUnicodeEscape(value);
                default -> throw error("未知字符串转义: \\" + escaped);
            }
        }

        private void appendUnicodeEscape(StringBuilder value) {
            char character = readHexCharacter();
            if (Character.isHighSurrogate(character)) {
                if (index + 1 >= input.length() || input.charAt(index) != '\\' || input.charAt(index + 1) != 'u') {
                    throw error("高代理项后必须跟随低代理项");
                }
                index += 2;
                char low = readHexCharacter();
                if (!Character.isLowSurrogate(low)) {
                    throw error("高代理项后必须跟随低代理项");
                }
                value.append(character).append(low);
            } else if (Character.isLowSurrogate(character)) {
                throw error("字符串包含孤立低代理项");
            } else {
                value.append(character);
            }
        }

        private char readHexCharacter() {
            if (index + 4 > input.length()) {
                throw error("Unicode 转义必须有四位十六进制数字");
            }
            int value = 0;
            for (int count = 0; count < 4; count++) {
                int digit = Character.digit(input.charAt(index++), 16);
                if (digit < 0) {
                    throw error("Unicode 转义包含非十六进制字符");
                }
                value = value * 16 + digit;
            }
            return (char) value;
        }

        private BigDecimal parseNumber() {
            int start = index;
            consume('-');
            if (atEnd()) {
                throw error("数字不完整");
            }
            if (consume('0')) {
                if (!atEnd() && Character.isDigit(input.charAt(index))) {
                    throw error("数字不能有前导零");
                }
            } else {
                if (atEnd() || input.charAt(index) < '1' || input.charAt(index) > '9') {
                    throw error("非法 JSON 值");
                }
                while (!atEnd() && Character.isDigit(input.charAt(index))) {
                    index++;
                }
            }
            if (consume('.')) {
                requireDigits("小数点后必须有数字");
            }
            if (!atEnd() && (input.charAt(index) == 'e' || input.charAt(index) == 'E')) {
                index++;
                if (!atEnd() && (input.charAt(index) == '+' || input.charAt(index) == '-')) {
                    index++;
                }
                requireDigits("指数部分必须有数字");
            }
            try {
                return new BigDecimal(input.substring(start, index));
            } catch (NumberFormatException exception) {
                throw error("非法 JSON 数字");
            }
        }

        private void requireDigits(String message) {
            int start = index;
            while (!atEnd() && Character.isDigit(input.charAt(index))) {
                index++;
            }
            if (start == index) {
                throw error(message);
            }
        }

        private Object parseLiteral(String literal, Object value) {
            if (!input.startsWith(literal, index)) {
                throw error("非法 JSON 值");
            }
            index += literal.length();
            return value;
        }

        private void checkDepth(int depth) {
            if (depth >= MAX_DEPTH) {
                throw error("JSON 嵌套深度超过 " + MAX_DEPTH);
            }
        }

        private void skipWhitespace() {
            while (!atEnd()) {
                char character = input.charAt(index);
                if (character != ' ' && character != '\n' && character != '\r' && character != '\t') {
                    return;
                }
                index++;
            }
        }

        private boolean consume(char expected) {
            if (!atEnd() && input.charAt(index) == expected) {
                index++;
                return true;
            }
            return false;
        }

        private void require(char expected) {
            if (!consume(expected)) {
                throw error("需要字符 '" + expected + "'");
            }
        }

        private boolean atEnd() {
            return index >= input.length();
        }

        private IllegalArgumentException error(String message) {
            return new IllegalArgumentException(message + "（位置 " + index + "）");
        }
    }
}
