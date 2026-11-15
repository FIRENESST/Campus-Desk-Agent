import unittest


class PublicApiTests(unittest.TestCase):
    def test_protocol_adapters_and_composite_are_publicly_exported(self):
        import main_agent

        names = [
            "CompositeToolClient",
            "CompositeToolError",
            "DuplicateToolError",
            "MintaGatewayClient",
            "MintaResponse",
            "MintaError",
            "MintaProtocolError",
            "MintaRequestError",
            "RestToolClient",
            "RestToolError",
            "RestProtocolError",
            "RestRequestError",
            "GraphQLToolClient",
            "GraphQLToolError",
            "GraphQLProtocolError",
            "GraphQLRequestError",
            "WebSocketToolClient",
            "WebSocketToolError",
            "WebSocketProtocolError",
            "WebSocketRequestError",
        ]

        for name in names:
            with self.subTest(name=name):
                self.assertIsNotNone(getattr(main_agent, name))
                self.assertIn(name, main_agent.__all__)


if __name__ == "__main__":
    unittest.main()
