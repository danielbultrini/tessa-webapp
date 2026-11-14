"""IBM Quantum samplers -- real hardware, or a fake-provider calibration snapshot.

Thin on purpose: ``qiskit_ibm_runtime.SamplerV2`` already satisfies the sampler
protocol (``.backend()`` + ``.run()``), so this module only resolves *which*
device -- and, for real hardware, the credentials that reach it -- and hands back
the primitive.  Requires the ``ibm`` extra.

Nothing here is a secret by itself: :data:`DEFAULT_INSTANCE_CRN` is an *instance
identifier* for an IBM Cloud open-plan instance, not a token.  It is kept as a
documented default so the notebooks read cleanly, but both it and the API token
are overridable from the environment:

======================  ==============================================
``IQPIXL_IBM_INSTANCE``  IBM Cloud instance CRN (defaults to the value below)
``QISKIT_IBM_TOKEN``     IBM Quantum API token (no default; also read by Qiskit)
``IQPIXL_IBM_TOKEN``     alternative token variable, checked first
======================  ==============================================

**Never commit an API token.**  Export it instead::

    export QISKIT_IBM_TOKEN="..."

Hardware submission is opt-in throughout: nothing contacts IBM until you build a
real sampler and run it.
"""

from __future__ import annotations

import os

__all__ = [
    "DEFAULT_INSTANCE_CRN",
    "fake_backend",
    "fake_sampler",
    "ibm_instance",
    "ibm_sampler",
    "ibm_token",
    "make_service",
]

#: Open-plan IBM Cloud instance CRN used for the runs reported in the paper.
DEFAULT_INSTANCE_CRN = (
    "crn:v1:bluemix:public:quantum-computing:us-east:"
    "a/22dc8fdbcf474c5c8b2faee4cf250adb:6c7d0adb-5937-450c-bc5d-8dfcd0c8c962::"
)


def ibm_instance(instance: str | None = None) -> str:
    """Resolve the instance CRN: argument -> ``IQPIXL_IBM_INSTANCE`` -> default."""
    return instance or os.environ.get("IQPIXL_IBM_INSTANCE") or DEFAULT_INSTANCE_CRN


def ibm_token(token: str | None = None) -> str:
    """Resolve the API token: argument -> ``IQPIXL_IBM_TOKEN`` -> ``QISKIT_IBM_TOKEN``.

    Raises
    ------
    RuntimeError
        If no token is available.  Set the environment variable rather than
        hard-coding a token in a notebook.
    """
    tok = (
        token or os.environ.get("IQPIXL_IBM_TOKEN") or os.environ.get("QISKIT_IBM_TOKEN")
    )
    if not tok:
        raise RuntimeError(
            "no IBM Quantum API token found; export QISKIT_IBM_TOKEN=... "
            "(or pass token=...) before submitting hardware jobs"
        )
    return tok


def make_service(token: str | None = None, instance: str | None = None):
    """Create a ``QiskitRuntimeService`` for the IBM Quantum Platform (IBM Cloud).

    ``token`` and ``instance`` fall back to the environment (see the module
    docstring).
    """
    from qiskit_ibm_runtime import QiskitRuntimeService

    return QiskitRuntimeService(
        channel="ibm_quantum_platform",
        token=ibm_token(token),
        instance=ibm_instance(instance),
    )


def ibm_sampler(
    backend=None,
    *,
    service=None,
    name: str | None = None,
    token: str | None = None,
    instance: str | None = None,
    **options,
):
    """A Runtime ``SamplerV2`` bound to a real IBM device.

    Give it a ``backend`` object directly, or a ``name`` to resolve one -- via
    ``service`` if you already have one, else via :func:`make_service` with
    ``token``/``instance`` (both falling back to the environment).  ``options``
    are passed to ``SamplerV2``.

    Submitting a job is the caller's decision: nothing here contacts IBM until
    the returned sampler is actually run -- except resolving ``name``, which
    needs the service.
    """
    from qiskit_ibm_runtime import SamplerV2

    if backend is None:
        if name is None:
            raise ValueError("pass a backend object, or a name to resolve")
        if service is None:
            service = make_service(token=token, instance=instance)
        backend = service.backend(name)
    return SamplerV2(mode=backend, **options)


def fake_backend(name: str):
    """A ``qiskit_ibm_runtime.fake_provider`` backend by name.

    Accepts either the class name or its snake-case form: ``"FakeFez"``,
    ``"fake_fez"`` and ``"fez"`` all resolve to ``FakeFez``.
    """
    from qiskit_ibm_runtime import fake_provider

    candidates = [name, f"Fake{name.title().replace('_', '')}"]
    if name.lower().startswith("fake_"):
        candidates.append("Fake" + name[5:].title().replace("_", ""))
    for candidate in candidates:
        cls = getattr(fake_provider, candidate, None)
        if isinstance(cls, type):  # the module also exposes same-named submodules
            return cls()
    raise ValueError(f"no fake backend matching {name!r} in qiskit_ibm_runtime")


def fake_sampler(name: str, *, local: bool = True, seed=None, **kwargs):
    """Noisy local sampler standing in for IBM chip ``name``.

    Shorthand for :func:`iqpixl.backends.calibrated_sampler` on
    :func:`fake_backend` -- ``fake_sampler("fez")`` runs on ``FakeFez``'s
    topology with ``FakeFez``'s calibration, entirely offline.
    """
    from .aer import calibrated_sampler

    return calibrated_sampler(fake_backend(name), local=local, seed=seed, **kwargs)
