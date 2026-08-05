class ControlPlaneError(Exception):
    """Base error mapped to a safe internal API response."""


class InvalidEnrollment(ControlPlaneError):
    pass


class EnrollmentReplay(ControlPlaneError):
    pass


class WorkerIdentityCollision(ControlPlaneError):
    pass


class RegistrationLeaseLost(ControlPlaneError):
    pass


class NoWorkerCapacity(ControlPlaneError):
    pass


class SlotLeaseLost(ControlPlaneError):
    pass
