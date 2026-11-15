package library;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;

public final class JsonTest {
    public static void main(String[] args) {
        parsesAndSerializesEveryJsonType();
        rejectsMalformedAndExcessivelyNestedInput();
        System.out.println("JsonTest: PASS");
    }

    private static void parsesAndSerializesEveryJsonType() {
        String source = "{\"text\":\"引号\\\" 反斜杠\\\\ 换行\\n 雪花\\u96ea\",\"items\":[1,-2.5,1e2,true,false,null,{\"ok\":\"\\/\"}]}";
        Object parsed = Json.parse(source);
        if (!(parsed instanceof Map<?, ?> object)) {
            throw new AssertionError("根节点应为对象");
        }
        if (!"引号\" 反斜杠\\ 换行\n 雪花雪".equals(object.get("text"))) {
            throw new AssertionError("字符串转义或 Unicode 解析错误: " + object.get("text"));
        }
        List<?> items = (List<?>) object.get("items");
        if (items.size() != 7 || new BigDecimal("1").compareTo((BigDecimal) items.get(0)) != 0
                || new BigDecimal("-2.5").compareTo((BigDecimal) items.get(1)) != 0
                || new BigDecimal("100").compareTo((BigDecimal) items.get(2)) != 0
                || !Boolean.TRUE.equals(items.get(3)) || !Boolean.FALSE.equals(items.get(4))
                || items.get(5) != null) {
            throw new AssertionError("数组、数字、布尔或 null 解析错误");
        }
        String encoded = Json.stringify(parsed);
        Object reparsed = Json.parse(encoded);
        if (!encoded.equals(Json.stringify(reparsed))) {
            throw new AssertionError("JSON 往返序列化不稳定: " + encoded);
        }
        if (!"\"\\u0001\\b\\f\\r\\t\"".equals(Json.stringify("\u0001\b\f\r\t"))) {
            throw new AssertionError("控制字符未正确转义");
        }
    }

    private static void rejectsMalformedAndExcessivelyNestedInput() {
        String[] malformed = {
            "", "{", "[1,]", "{\"a\":1,}", "{\"a\":01}", "true false",
            "\"\\x\"", "nul", "1.", ".1", "+1", "\"\n\"", "\"\\uD800\""
        };
        for (String input : malformed) {
            expectInvalid(input);
        }
        expectInvalid("[".repeat(65) + "null" + "]".repeat(65));
    }

    private static void expectInvalid(String input) {
        try {
            Json.parse(input);
            throw new AssertionError("应拒绝畸形 JSON: " + input);
        } catch (IllegalArgumentException expected) {
            // Expected rejection.
        }
    }
}
