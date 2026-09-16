"""Immutable oracle for juniper-green:corrections."""
import service


def main():
    assert service.current_timeout() == 174, service.current_timeout()


if __name__ == "__main__":
    main()
