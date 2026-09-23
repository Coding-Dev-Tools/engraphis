"""Immutable oracle for cinder-green:corrections."""
import service


def main():
    assert service.current_timeout() == 90, service.current_timeout()


if __name__ == "__main__":
    main()
