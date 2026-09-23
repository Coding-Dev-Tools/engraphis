"""Immutable oracle for helios-west:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-helios-west', service.current_policy()


if __name__ == "__main__":
    main()
