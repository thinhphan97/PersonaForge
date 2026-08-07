from scripts import devops


def test_worker_status_formats_raw_child_resource_fields(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        devops,
        "get_inventory",
        lambda _env: [
            {
                "worker_id": "worker-1",
                "status": "ready",
                "heartbeat": {
                    "hardware_source": "nvitop",
                    "child_resources": [
                        {
                            "session_id": "session-1",
                            "pid": 42,
                            "assigned_gpu_uuid": "GPU-1",
                            "gpu_memory_bytes": 1_222_639_616,
                            "gpu_sm_utilization": 7.5,
                            "cpu_utilization": 63.0,
                            "host_rss_bytes": 1_325_064_192,
                            "process_identity_matches": True,
                        }
                    ],
                },
            }
        ],
    )

    assert devops.command_worker_status({}, capacity=False, children=True) == 0

    output = capsys.readouterr().out
    assert "gpu=GPU-1" in output
    assert "vram=1166.0MiB" in output
    assert "gpu_util=7.5%" in output
    assert "ram=1263.7MiB" in output


def test_worker_capacity_shows_used_and_free_slots_per_gpu(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        devops,
        "get_inventory",
        lambda _env: [
            {
                "worker_id": "worker-1",
                "status": "ready",
                "capabilities": {
                    "gpus": [
                        {"gpu_uuid": "GPU-1", "max_slots": 4},
                        {"gpu_uuid": "GPU-2", "max_slots": 4},
                    ]
                },
                "heartbeat": {
                    "hardware_source": "nvitop",
                    "gpu_resources": [
                        {"gpu_uuid": "GPU-1", "healthy": True},
                        {"gpu_uuid": "GPU-2", "healthy": True},
                    ],
                    "child_resources": [
                        {"assigned_gpu_uuid": "GPU-1"},
                        {"assigned_gpu_uuid": "GPU-1"},
                        {"assigned_gpu_uuid": "GPU-2"},
                    ],
                },
            }
        ],
    )

    assert devops.command_worker_status({}, capacity=True, children=False) == 0

    output = capsys.readouterr().out
    assert "gpu=GPU-1 slots=4 used_slots=2 free_slots=2" in output
    assert "gpu=GPU-2 slots=4 used_slots=1 free_slots=3" in output
