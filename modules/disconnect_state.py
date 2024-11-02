# Handling intentional disconnects from the user and the timeout function.

class DisconnectState:
    def __init__(self):
        self.intentional_disconnect = False

    def set_intentional(self):
        self.intentional_disconnect = True

    def clear(self):
        self.intentional_disconnect = False

    def is_intentional(self):
        return self.intentional_disconnect
