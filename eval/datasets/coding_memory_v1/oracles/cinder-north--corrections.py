"""Immutable oracle for cinder-north:corrections."""
import service


def main():
    assert service.current_timeout() == 82, service.current_timeout()


if __name__ == "__main__":
    main()
