"""Immutable oracle for cinder-north:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-cinder-north', service.current_policy()


if __name__ == "__main__":
    main()
